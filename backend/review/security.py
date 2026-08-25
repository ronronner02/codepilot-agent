"""安全可疑模式的候选点定位。固定模式集匹配，不让 LLM 自由发现。

为什么用固定模式集：「让 LLM 找安全问题」会产出大量看起来合理但无法核验的断言，而
安全类发现的误报代价特别高——读者花时间确认后发现不是问题，就不再相信后续任何一条。
固定模式集的每条命中都能指出「凭哪个模式匹配到的」，可核验。

**检测分两层，因为覆盖范围不同：**

  文本层   硬编码密钥。必须能扫 `.env`、`*.pem`、`*.key` 这类文件——它们不是
           Python/TypeScript，tree-sitter 解析不了，但恰恰最可能含密钥。
           DoD 明确要求「密钥文件形态不产生切块，Reviewer 仍扫描这些文件」：
           索引层排除它们是为了不让问答链路把密钥检索出来，与评审要不要看是两件事。

  AST 层   SQL 拼接、命令拼接、不安全反序列化。这些要看语法结构才能区分
           「拼接字符串」与「参数化查询」，正则做不到。

模式集是可枚举的（见 SECRET_PATTERNS 与各检测器的名单），零命中时按 AE2 把它列进
产出——「查了这些模式，都没命中」与「没查」在读者看来必须不同。
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import tree_sitter as ts

from backend.paths import PathEscapeError, resolve_within
from backend.review.models import Candidate, Confidence, FindingCategory
from backend.static_analysis.parser import ParsedTree, node_text

# ── 硬编码密钥的模式集 ────────────────────────────────────────────────
#
# 每条带名字，因为零命中时要把「查过哪些模式」写进产出（AE2）。
#
# 两类模式：一类匹配特定服务的密钥格式（前缀固定，几乎无误报），一类匹配
# 「赋值给密钥类变量名的字面量」（通用，但需要熵检查压误报）。
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "aws_access_key_id",
        re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
        "AWS 访问密钥 ID 的固定格式（AKIA/ASIA 前缀加 16 位大写字母数字）",
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
        "GitHub 令牌的固定格式（ghp_/gho_/ghu_/ghs_/ghr_ 前缀）",
    ),
    (
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "OpenAI 风格密钥格式（sk- 前缀）",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        "Slack 令牌格式（xox 前缀）",
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "PEM 私钥块的起始标记",
    ),
    (
        "assigned_credential",
        re.compile(
            # 名字允许带前缀：`DB_PASSWORD`、`MY_API_KEY`、`APP_SECRET` 是 .env 里最
            # 常见的形态。用 `\b` 锚定会全部漏掉——下划线是词字符，`DB_` 与
            # `PASSWORD` 之间没有词边界。
            #
            # 引号可选：`.env` 的惯例是不带引号（`DB_PASSWORD=xxx`），而这正是 DoD
            # 点名要扫的文件类型。只匹配带引号的值会漏掉它最常见的写法。
            # 无引号时值以空白、`#`（行内注释）或行尾终止。
            r"""(?ix)
            (?P<name>[A-Za-z0-9_.-]*
                (?:password|passwd|secret|api[_-]?key|apikey|token|
                   access[_-]?key|private[_-]?key|client[_-]?secret))
            \s*[:=]\s*
            (?:
                (?P<quote>['"])(?P<value>[^'"\n]{8,})(?P=quote)
              | (?P<bare>[^\s#'"][^\s#]{7,})
            )
            """
        ),
        "密钥类变量名被赋以字面量字符串（长度 >= 8）",
    ),
)

# 占位值。匹配到这些不算密钥——示例文件与模板里全是它们。
_PLACEHOLDER_VALUES = frozenset(
    {
        "changeme",
        "change_me",
        "your_api_key",
        "your-api-key",
        "yourkeyhere",
        "placeholder",
        "example",
        "password",
        "secret",
        "token",
        "xxxxxxxx",
        "todo",
        "none",
        "null",
        "dummy",
        "test",
        "fake",
        "sample",
        "redacted",
    }
)

# 协议与框架里的知名非密钥值。
#
# 实测教训：`HEADER_KEY_TOKEN = 'Authorization'` 被判为密钥——变量名含 token，值的
# 字符熵 3.2 刚好过线。这类常量在任何 Web 项目里都有，不过滤会持续误报。
_WELL_KNOWN_NON_SECRETS = frozenset(
    {
        "authorization",
        "bearer",
        "basic",
        "content-type",
        "application/json",
        "x-api-key",
        "set-cookie",
        "www-authenticate",
        "proxy-authorization",
        "authentication",
        "credentials",
        "same-origin",
        "no-cache",
    }
)

# 通用模式的熵下限。真实密钥是高熵随机串，配置项的值通常是词或路径。
#
# 取 2.5 的依据：8 个不同字符的均匀分布熵为 3.0，"password" 这类重复字母多的词约
# 2.75，而 base64 随机串通常在 4.5 以上。2.5 能滤掉大部分自然语言值，又不至于漏掉
# 较短的真实密钥。这是启发式阈值，作为参数暴露。
DEFAULT_MIN_SECRET_ENTROPY = 3.2

# 密钥形态文件的扩展名与文件名。这些文件即使不可解析也要扫（DoD 要求）。
SECRET_SHAPED_NAMES = frozenset({".env", ".env.local", ".env.production", ".npmrc", ".netrc"})
SECRET_SHAPED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})

# ── AST 层的名单 ──────────────────────────────────────────────────────

_SQL_KEYWORDS = re.compile(
    r"(?i)\b(select|insert\s+into|update|delete\s+from|where|from|join|union)\b"
)

# 执行 SQL 的方法名。命中这些且首参是拼接出来的字符串即为候选。
_SQL_EXECUTORS = frozenset({"execute", "executemany", "executescript", "query", "raw"})

# 执行 shell 命令的调用。
_PY_SHELL_CALLS = frozenset({"system", "popen", "getoutput", "getstatusoutput"})
_PY_SUBPROCESS_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
_TS_SHELL_CALLS = frozenset({"exec", "execSync", "spawnSync", "spawn"})

# 不安全的反序列化与求值。
_PY_UNSAFE_DESERIALIZE = frozenset({"loads", "load"})
_PY_UNSAFE_MODULES = frozenset({"pickle", "cPickle", "dill", "shelve", "marshal"})
_PY_UNSAFE_EVAL = frozenset({"eval", "exec"})
_TS_UNSAFE_EVAL = frozenset({"eval", "Function"})

_SNIPPET_RADIUS = 2


def _shannon_entropy(value: str) -> float:
    """字符级香农熵。用于区分随机密钥与自然语言值。"""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def is_secret_shaped_file(rel_path: str) -> bool:
    """路径是否属于密钥形态文件。

    这类文件不入向量索引，但 Reviewer 仍要扫——两件事的目的不同：索引排除是防止
    问答链路把密钥检索出来，评审扫描是为了报告它们存在。
    """
    path = Path(rel_path)
    return path.name in SECRET_SHAPED_NAMES or path.suffix.lower() in SECRET_SHAPED_SUFFIXES


def _normalize_identifier(text: str) -> str:
    """去掉分隔符并转小写，用于比较名字与值是否实质相同。"""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip().strip("<>{}[]").lower()
    if stripped in _PLACEHOLDER_VALUES or stripped in _WELL_KNOWN_NON_SECRETS:
        return True
    # 环境变量引用与模板占位不是硬编码。
    if stripped.startswith(("$", "${", "%(", "{{", "os.environ", "process.env")):
        return True
    # 全是同一个字符（xxxxxxxx、********）。
    return len(set(stripped)) <= 2


def _value_mirrors_name(name: str, value: str) -> bool:
    """值是否只是名字的复述。

    实测教训：`enableAuthToken: 'enableAuthToken'` 与
    `updateOwnAccessToken: 'updateOwnAccessToken'` 这类权限枚举被判为密钥——变量名含
    token，而 camelCase 标识符的字符熵本就有 3.5-3.8，熵过滤完全挡不住。

    熵衡量的是字符随机性，不是「这是不是一个有意义的标识符」。名字与值相同（或一方
    包含另一方）是枚举、常量映射、i18n 键的典型形态，绝不会是密钥——密钥不可能等于
    存放它的变量名。这是熵启发式的必要互补。
    """
    normalized_name = _normalize_identifier(name)
    normalized_value = _normalize_identifier(value)
    if not normalized_name or not normalized_value:
        return False
    return normalized_name == normalized_value or (
        len(normalized_value) >= 6
        and (normalized_value in normalized_name or normalized_name in normalized_value)
    )


def _redact(value: str) -> str:
    """产出里不回显密钥全文。

    评审报告会被贴到别处、进日志、存成文件。把密钥原文写进去等于再泄露一次。
    只留首尾各 3 个字符供定位。
    """
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def find_hardcoded_secrets(
    rel_path: str,
    text: str,
    min_entropy: float = DEFAULT_MIN_SECRET_ENTROPY,
) -> list[Candidate]:
    """文本层扫描硬编码密钥。任何文件都能扫，不要求可解析。

    特定格式的模式（AWS、GitHub 等）直接命中即报——前缀固定，误报极少。
    通用模式（密钥类变量名赋字面量）要过占位值与熵两道过滤，否则 `.env.example`
    与测试固件会淹没真实发现。
    """
    lines = text.splitlines()
    candidates: list[Candidate] = []

    for name, pattern, description in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            line_number = text[: match.start()].count("\n") + 1

            if name == "assigned_credential":
                # 引号可选，故值可能落在两个捕获组之一。
                value = match.group("value") or match.group("bare") or ""
                if not value or _looks_like_placeholder(value):
                    continue
                if _value_mirrors_name(match.group("name"), value):
                    continue
                entropy = _shannon_entropy(value)
                if entropy < min_entropy:
                    continue
                evidence = (
                    f"模式 {name}：{description}；变量名 {match.group('name')}，"
                    f"值 {_redact(value)}（字符熵 {entropy:.1f} >= {min_entropy}）"
                )
                # 只打码值本身，保留变量名——变量名是定位信息，打掉它片段就没用了。
                secret_text = value
            else:
                secret_text = match.group(0)
                evidence = f"模式 {name}：{description}；匹配到 {_redact(secret_text)}"

            start = max(0, line_number - 1 - _SNIPPET_RADIUS)
            end = min(len(lines), line_number + _SNIPPET_RADIUS)
            candidates.append(
                Candidate(
                    category=FindingCategory.SECURITY,
                    kind="hardcoded_secret",
                    path=rel_path,
                    line=line_number,
                    # 片段也脱敏：它同样进产出。
                    snippet="\n".join(
                        line.replace(secret_text, _redact(secret_text))
                        for line in lines[start:end]
                    ),
                    detector_evidence=evidence,
                    confidence=Confidence.CERTAIN
                    if name != "assigned_credential"
                    else Confidence.CONTEXTUAL,
                )
            )

    candidates.sort(key=lambda c: (c.line, c.detector_evidence))
    return candidates


def _walk(node: ts.Node) -> list[ts.Node]:
    collected = [node]
    index = 0
    while index < len(collected):
        collected.extend(collected[index].children)
        index += 1
    return collected


def _snippet(source: bytes, node: ts.Node) -> str:
    lines = source.decode("utf-8", errors="replace").splitlines()
    start = max(0, node.start_point[0] - _SNIPPET_RADIUS)
    end = min(len(lines), node.end_point[0] + _SNIPPET_RADIUS + 1)
    return "\n".join(lines[start:end])


def _call_parts(call: ts.Node, source: bytes) -> tuple[str, str]:
    """取调用的 (对象名, 方法名)。非属性调用时对象名为空串。"""
    function = call.child_by_field_name("function")
    if function is None:
        return "", ""
    if function.type in ("attribute", "member_expression"):
        obj = function.child_by_field_name("object")
        attribute = function.child_by_field_name("attribute") or function.child_by_field_name(
            "property"
        )
        return (
            node_text(source, obj) if obj is not None else "",
            node_text(source, attribute) if attribute is not None else "",
        )
    return "", node_text(source, function)


def _is_dynamic_string(node: ts.Node, source: bytes) -> tuple[bool, str]:
    """节点是否是「拼接或插值出来的字符串」。返回 (是否, 形态描述)。

    三种形态都要认（实测确认的节点结构）：
      "a" + var          binary_operator，operator 为 +，left 是 string
      f"a{var}"          string 含 interpolation 子节点
      "a %s" % var       binary_operator，operator 为 %
      `a${var}`          TS 的 template_string 含 template_substitution
    """
    if node.type == "binary_operator":
        operator = next((c for c in node.children if not c.is_named), None)
        symbol = node_text(source, operator) if operator is not None else ""
        if symbol in ("+", "%"):
            left = node.child_by_field_name("left")
            if left is not None and left.type in ("string", "concatenated_string"):
                return True, f"字符串与变量用 {symbol} 拼接"
    if node.type == "binary_expression":
        operator = next((c for c in node.children if not c.is_named), None)
        if operator is not None and node_text(source, operator) == "+":
            left = node.child_by_field_name("left")
            if left is not None and left.type in ("string", "template_string"):
                return True, "字符串与变量用 + 拼接"
    if node.type == "string" and any(
        child.type == "interpolation" for child in node.named_children
    ):
        return True, "f-string 插值"
    if node.type == "template_string" and any(
        child.type == "template_substitution" for child in node.named_children
    ):
        return True, "模板字符串插值"
    return False, ""


def find_sql_injection(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """SQL 字符串拼接。

    判据是「执行 SQL 的调用，其首参是拼接或插值出来的字符串」。参数化查询传的是
    含占位符的字符串字面量加独立的参数元组，首参不是拼接结果，因此不命中——这正是
    要区分的两种形态，也是必须走 AST 而非正则的原因。

    另一条路径：赋值给变量的 SQL 字符串被拼接。这类不一定被执行，但足以进候选。
    """
    call_type = "call" if parsed.grammar == "python" else "call_expression"
    candidates: list[Candidate] = []

    for node in nodes:
        if node.type == call_type:
            _, method = _call_parts(node, parsed.source)
            if method not in _SQL_EXECUTORS:
                continue
            arguments = node.child_by_field_name("arguments")
            if arguments is None or not arguments.named_children:
                continue
            first = arguments.named_children[0]
            dynamic, shape = _is_dynamic_string(first, parsed.source)
            if not dynamic:
                continue
            candidates.append(
                Candidate(
                    category=FindingCategory.SECURITY,
                    kind="sql_injection",
                    path=parsed.path,
                    line=node.start_point[0] + 1,
                    snippet=_snippet(parsed.source, node),
                    detector_evidence=(
                        f"{method}() 的首个参数是{shape}而非参数化占位符，"
                        "变量值会直接进入 SQL 语句"
                    ),
                    confidence=Confidence.CERTAIN,
                )
            )
            continue

        # 变量赋值形态：q = "SELECT ... " + uid
        if node.type != "assignment":
            continue
        value = node.child_by_field_name("right")
        if value is None:
            continue
        dynamic, shape = _is_dynamic_string(value, parsed.source)
        if not dynamic or not _SQL_KEYWORDS.search(node_text(parsed.source, value)):
            continue
        candidates.append(
            Candidate(
                category=FindingCategory.SECURITY,
                kind="sql_injection",
                path=parsed.path,
                line=node.start_point[0] + 1,
                snippet=_snippet(parsed.source, node),
                detector_evidence=(
                    f"含 SQL 关键字的字符串通过{shape}构造。"
                    "需确认该字符串是否最终被执行，以及变量是否来自外部输入"
                ),
                confidence=Confidence.CONTEXTUAL,
            )
        )
    return candidates


def find_command_injection(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """命令拼接。

    两种命中形态：
      os.system("cmd " + var)         命令字符串由拼接构成
      subprocess.run(..., shell=True) shell=True 时字符串经 shell 解析

    `subprocess.run(["cat", path])` 传列表不经 shell，不命中——这是安全写法，
    报出来就是误报。
    """
    call_type = "call" if parsed.grammar == "python" else "call_expression"
    candidates: list[Candidate] = []

    for node in nodes:
        if node.type != call_type:
            continue
        obj, method = _call_parts(node, parsed.source)
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            continue

        is_shell_call = method in _PY_SHELL_CALLS or method in _TS_SHELL_CALLS
        is_subprocess = method in _PY_SUBPROCESS_CALLS and obj in ("subprocess", "sp")

        shell_true = any(
            "shell" in node_text(parsed.source, child) and "True" in node_text(parsed.source, child)
            for child in arguments.named_children
            if child.type == "keyword_argument"
        )

        first = arguments.named_children[0] if arguments.named_children else None
        dynamic, shape = (
            _is_dynamic_string(first, parsed.source) if first is not None else (False, "")
        )

        if is_shell_call and dynamic:
            evidence = f"{method}() 的命令字符串通过{shape}构造，变量内容会被 shell 解析"
        elif is_subprocess and shell_true:
            evidence = (
                f"subprocess.{method}() 使用 shell=True，命令字符串经 shell 解析；"
                "传列表参数可避免"
            )
        elif is_shell_call and first is not None and first.type in ("string", "template_string"):
            # 纯字面量命令没有注入面，跳过。
            continue
        else:
            continue

        candidates.append(
            Candidate(
                category=FindingCategory.SECURITY,
                kind="command_injection",
                path=parsed.path,
                line=node.start_point[0] + 1,
                snippet=_snippet(parsed.source, node),
                detector_evidence=evidence,
                confidence=Confidence.CERTAIN if dynamic else Confidence.CONTEXTUAL,
            )
        )
    return candidates


def find_unsafe_deserialization(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """不安全反序列化与动态求值。

    `pickle.loads` 能在反序列化过程中执行任意代码，`yaml.load` 不传 SafeLoader 时
    同理，`eval`/`exec` 直接执行。这几个的危害不取决于上下文，故为 CERTAIN。
    """
    call_type = "call" if parsed.grammar == "python" else "call_expression"
    candidates: list[Candidate] = []

    for node in nodes:
        if node.type != call_type:
            continue
        obj, method = _call_parts(node, parsed.source)
        arguments = node.child_by_field_name("arguments")
        argument_text = node_text(parsed.source, arguments) if arguments is not None else ""

        evidence = ""
        if parsed.grammar == "python":
            if obj in _PY_UNSAFE_MODULES and method in _PY_UNSAFE_DESERIALIZE:
                evidence = (
                    f"{obj}.{method}() 在反序列化过程中可执行任意代码，"
                    "不应用于不可信输入"
                )
            elif obj == "yaml" and method == "load" and "Loader" not in argument_text:
                evidence = (
                    "yaml.load() 未指定 SafeLoader，默认加载器可构造任意 Python 对象"
                )
            elif not obj and method in _PY_UNSAFE_EVAL:
                evidence = f"{method}() 直接执行传入的字符串"
        elif method in _TS_UNSAFE_EVAL and not obj:
            evidence = f"{method}() 直接执行传入的字符串"

        if not evidence:
            continue

        candidates.append(
            Candidate(
                category=FindingCategory.SECURITY,
                kind="unsafe_deserialization",
                path=parsed.path,
                line=node.start_point[0] + 1,
                snippet=_snippet(parsed.source, node),
                detector_evidence=evidence,
                confidence=Confidence.CERTAIN,
            )
        )
    return candidates


def find_security_candidates(parsed: ParsedTree) -> list[Candidate]:
    """跑 AST 层的三类安全检测。密钥检测走 find_hardcoded_secrets（文本层）。"""
    nodes = _walk(parsed.tree.root_node)
    candidates = [
        *find_sql_injection(parsed, nodes),
        *find_command_injection(parsed, nodes),
        *find_unsafe_deserialization(parsed, nodes),
    ]
    candidates.sort(key=lambda c: (c.line, c.kind))
    return candidates


def describe_pattern_set() -> str:
    """可陈述的模式集描述。零命中时按 AE2 写进产出。"""
    secret_names = ", ".join(name for name, _, _ in SECRET_PATTERNS)
    return (
        f"硬编码密钥（文本层，{len(SECRET_PATTERNS)} 个模式：{secret_names}）；"
        f"SQL 字符串拼接（执行方法：{', '.join(sorted(_SQL_EXECUTORS))}）；"
        f"命令拼接（shell 调用与 subprocess shell=True）；"
        f"不安全反序列化（{', '.join(sorted(_PY_UNSAFE_MODULES))}、yaml.load、eval/exec）"
    )
