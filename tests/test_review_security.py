"""安全可疑模式的候选点定位。

安全类误报代价特别高：读者花时间确认后发现不是问题，就不再相信后续任何一条发现。
所以「参数化查询不命中」「传列表的 subprocess 不命中」这些边界与正向命中同等重要。

另一条贯穿本文件的约束：产出里不得回显密钥全文。评审报告会被贴到别处、进日志、
存成文件，把密钥原文写进去等于再泄露一次。
"""

from __future__ import annotations

from pathlib import Path

from backend.review.models import Confidence, FindingCategory
from backend.review.security import (
    SECRET_PATTERNS,
    describe_pattern_set,
    find_hardcoded_secrets,
    find_security_candidates,
    is_secret_shaped_file,
)
from backend.static_analysis.parser import ParsedTree, parse_tree

MAX_BYTES = 1_048_576


def _ast_candidates(tmp_path: Path, rel: str, body: str) -> list:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    parsed = parse_tree(tmp_path, rel, MAX_BYTES)
    assert isinstance(parsed, ParsedTree), f"预期建树成功，实际：{parsed}"
    return find_security_candidates(parsed)


def _kinds(candidates: list) -> set[str]:
    return {c.kind for c in candidates}


class TestHardcodedSecrets:
    def test_aws_key_located(self) -> None:
        found = find_hardcoded_secrets("cfg.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        assert len(found) == 1
        assert found[0].kind == "hardcoded_secret"
        assert found[0].confidence is Confidence.CERTAIN

    def test_github_token_located(self) -> None:
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        found = find_hardcoded_secrets(".env", f"GITHUB_TOKEN={token}\n")
        assert any("github_token" in c.detector_evidence for c in found)

    def test_private_key_block_located(self) -> None:
        found = find_hardcoded_secrets(
            "id_rsa", "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKC\n"
        )
        assert any("private_key_block" in c.detector_evidence for c in found)

    def test_secret_value_is_redacted_in_evidence(self) -> None:
        """产出里不得回显密钥全文——报告会进日志、被转贴。"""
        found = find_hardcoded_secrets("cfg.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        assert "AKIAIOSFODNN7EXAMPLE" not in found[0].detector_evidence
        assert "*" in found[0].detector_evidence

    def test_secret_value_is_redacted_in_snippet(self) -> None:
        """片段同样进产出，也要脱敏。"""
        found = find_hardcoded_secrets("cfg.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        assert "AKIAIOSFODNN7EXAMPLE" not in found[0].snippet

    def test_high_entropy_assignment_located(self) -> None:
        found = find_hardcoded_secrets("cfg.py", 'api_key = "xQ7fL2mZ9pR4tK8wB3nH"\n')
        assert any("assigned_credential" in c.detector_evidence for c in found)

    def test_placeholder_value_not_flagged(self) -> None:
        """`.env.example` 与模板里全是占位值，报出来会淹没真实发现。"""
        for value in ("changeme", "your_api_key", "placeholder", "xxxxxxxx"):
            found = find_hardcoded_secrets(".env.example", f'api_key = "{value}"\n')
            assert not found, f"占位值 {value} 不应命中"

    def test_env_reference_not_flagged(self) -> None:
        found = find_hardcoded_secrets("cfg.py", 'api_key = "${API_KEY}"\n')
        assert not found

    def test_low_entropy_value_not_flagged(self) -> None:
        """配置项的值通常是词或路径，熵低于真实密钥。"""
        found = find_hardcoded_secrets("cfg.py", 'password = "aaaaaaaaaa"\n')
        assert not found

    def test_enum_whose_value_mirrors_its_name_not_flagged(self) -> None:
        """权限枚举与常量映射的值等于自己的名字，不可能是密钥。

        基准仓库实测教训：ghostfolio 的 `enableAuthToken: 'enableAuthToken'` 与
        `updateOwnAccessToken: 'updateOwnAccessToken'` 被判为密钥。camelCase 标识符
        的字符熵有 3.5-3.8，熵过滤完全挡不住——熵衡量字符随机性，不是「这是不是一个
        有意义的标识符」。
        """
        source = (
            "export const permissions = {\n"
            "  enableAuthToken: 'enableAuthToken',\n"
            "  updateOwnAccessToken: 'updateOwnAccessToken',\n"
            "};\n"
        )
        assert find_hardcoded_secrets("permissions.ts", source) == []

    def test_well_known_header_name_not_flagged(self) -> None:
        """`HEADER_KEY_TOKEN = 'Authorization'` 在任何 Web 项目里都有。"""
        found = find_hardcoded_secrets(
            "config.ts", "export const HEADER_KEY_TOKEN = 'Authorization';\n"
        )
        assert found == []

    def test_real_secret_still_flagged_after_filters(self) -> None:
        """过滤不能宽到把真实密钥也放过。"""
        found = find_hardcoded_secrets("cfg.py", 'auth_token = "xQ7fL2mZ9pR4tK8wB3nH"\n')
        assert found, "真实随机密钥应仍然命中"

    def test_line_number_reported(self) -> None:
        text = 'x = 1\ny = 2\nKEY = "AKIAIOSFODNN7EXAMPLE"\n'
        assert find_hardcoded_secrets("cfg.py", text)[0].line == 3

    def test_works_on_unparseable_file_types(self) -> None:
        """密钥形态文件不是 Python/TS，tree-sitter 解析不了，但最可能含密钥。

        DoD 要求：密钥文件形态不产生切块，Reviewer 仍扫描这些文件。
        """
        found = find_hardcoded_secrets(".env", 'DB_PASSWORD="xQ7fL2mZ9pR4tK8wB3nH"\n')
        assert found

    def test_unquoted_env_value_matched(self) -> None:
        """`.env` 的惯例是不带引号，而它正是 DoD 点名要扫的文件类型。

        只匹配带引号的值会漏掉 `.env` 最常见的写法。
        """
        found = find_hardcoded_secrets(".env", "DB_PASSWORD=xQ7fL2mZ9pR4tK8wB3nH\n")
        assert found, "无引号的 .env 赋值未命中"

    def test_unquoted_value_stops_at_inline_comment(self) -> None:
        found = find_hardcoded_secrets(".env", "API_KEY=xQ7fL2mZ9pR4tK8wB3nH # 生产环境\n")
        assert found
        assert "生产环境" not in found[0].detector_evidence

    def test_variable_name_kept_in_snippet(self) -> None:
        """只打码值本身，保留变量名——变量名是定位信息，打掉片段就没用了。"""
        found = find_hardcoded_secrets(".env", "DB_PASSWORD=xQ7fL2mZ9pR4tK8wB3nH\n")
        assert "DB_PASSWORD" in found[0].snippet
        assert "xQ7fL2mZ9pR4tK8wB3nH" not in found[0].snippet

    def test_prefixed_variable_names_matched(self) -> None:
        """`DB_PASSWORD`、`MY_API_KEY` 是 .env 里最常见的形态。

        用 `\\b` 锚定关键词会全部漏掉——下划线是词字符，前缀与关键词之间没有词边界。
        """
        for name in ("DB_PASSWORD", "MY_API_KEY", "APP_SECRET", "service.access_key"):
            found = find_hardcoded_secrets(".env", f'{name}="xQ7fL2mZ9pR4tK8wB3nH"\n')
            assert found, f"{name} 未命中"

    def test_entropy_threshold_is_tunable(self) -> None:
        text = 'token = "abcdefgh12345678"\n'
        assert find_hardcoded_secrets("c.py", text, min_entropy=10.0) == []
        assert find_hardcoded_secrets("c.py", text, min_entropy=1.0)


class TestSecretShapedFiles:
    def test_env_and_pem_recognized(self) -> None:
        for path in (".env", "config/.env.production", "certs/server.pem", "id.key"):
            assert is_secret_shaped_file(path), path

    def test_ordinary_source_not_secret_shaped(self) -> None:
        for path in ("main.py", "src/app.ts", "README.md"):
            assert not is_secret_shaped_file(path), path


class TestSqlInjection:
    def test_concatenated_query_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "db.py", 'def go(uid):\n    cur.execute("SELECT * FROM t WHERE id = " + uid)\n'
        )
        sql = [c for c in found if c.kind == "sql_injection"]
        assert len(sql) == 1
        assert sql[0].confidence is Confidence.CERTAIN

    def test_fstring_query_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "db.py", 'def go(uid):\n    cur.execute(f"SELECT * FROM t WHERE id = {uid}")\n'
        )
        assert "sql_injection" in _kinds(found)

    def test_percent_format_query_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path,
            "db.py",
            'def go(uid):\n    cur.execute("SELECT * FROM t WHERE id = %s" % uid)\n',
        )
        assert "sql_injection" in _kinds(found)

    def test_parameterized_query_not_flagged(self, tmp_path: Path) -> None:
        """参数化查询传字面量加独立参数元组，首参不是拼接结果。

        这正是要区分的两种形态，也是必须走 AST 而非正则的原因。
        """
        found = _ast_candidates(
            tmp_path,
            "db.py",
            'def go(uid):\n    cur.execute("SELECT * FROM t WHERE id = %s", (uid,))\n',
        )
        assert "sql_injection" not in _kinds(found)

    def test_assigned_sql_string_is_contextual(self, tmp_path: Path) -> None:
        """赋值给变量的拼接 SQL 不一定被执行，置信度较低。"""
        found = _ast_candidates(
            tmp_path, "db.py", 'def go(uid):\n    q = "SELECT * FROM t WHERE id = " + uid\n    return q\n'
        )
        sql = [c for c in found if c.kind == "sql_injection"]
        assert sql
        assert sql[0].confidence is Confidence.CONTEXTUAL

    def test_non_sql_concatenation_not_flagged(self, tmp_path: Path) -> None:
        found = _ast_candidates(tmp_path, "m.py", 'def go(n):\n    msg = "hello " + n\n    return msg\n')
        assert "sql_injection" not in _kinds(found)


class TestCommandInjection:
    def test_os_system_with_concatenation_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "m.py", 'def go(path):\n    os.system("rm -rf " + path)\n'
        )
        cmd = [c for c in found if c.kind == "command_injection"]
        assert cmd
        assert cmd[0].confidence is Confidence.CERTAIN

    def test_subprocess_shell_true_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "m.py", 'def go(path):\n    subprocess.run(f"cat {path}", shell=True)\n'
        )
        assert "command_injection" in _kinds(found)

    def test_subprocess_with_list_not_flagged(self, tmp_path: Path) -> None:
        """传列表不经 shell，是安全写法，报出来就是误报。"""
        found = _ast_candidates(
            tmp_path, "m.py", 'def go(path):\n    subprocess.run(["cat", path])\n'
        )
        assert "command_injection" not in _kinds(found)

    def test_literal_command_not_flagged(self, tmp_path: Path) -> None:
        """纯字面量命令没有注入面。"""
        found = _ast_candidates(tmp_path, "m.py", 'def go():\n    os.system("ls -la")\n')
        assert "command_injection" not in _kinds(found)


class TestUnsafeDeserialization:
    def test_pickle_loads_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(tmp_path, "m.py", "def go(blob):\n    return pickle.loads(blob)\n")
        assert "unsafe_deserialization" in _kinds(found)

    def test_yaml_load_without_safe_loader_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(tmp_path, "m.py", "def go(text):\n    return yaml.load(text)\n")
        assert "unsafe_deserialization" in _kinds(found)

    def test_yaml_load_with_safe_loader_not_flagged(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "m.py", "def go(text):\n    return yaml.load(text, Loader=yaml.SafeLoader)\n"
        )
        assert "unsafe_deserialization" not in _kinds(found)

    def test_json_loads_not_flagged(self, tmp_path: Path) -> None:
        """json 反序列化不执行代码，不属于此类。"""
        found = _ast_candidates(tmp_path, "m.py", "def go(text):\n    return json.loads(text)\n")
        assert "unsafe_deserialization" not in _kinds(found)

    def test_eval_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(tmp_path, "m.py", "def go(expr):\n    return eval(expr)\n")
        assert "unsafe_deserialization" in _kinds(found)

    def test_typescript_eval_located(self, tmp_path: Path) -> None:
        found = _ast_candidates(tmp_path, "m.ts", "function go(expr: string) {\n  return eval(expr);\n}\n")
        assert "unsafe_deserialization" in _kinds(found)


class TestPatternSetDisclosure:
    def test_pattern_set_is_enumerable(self) -> None:
        """AE2 要求零命中时说明已检查的模式集合，所以它必须可陈述。"""
        described = describe_pattern_set()
        assert "硬编码密钥" in described
        assert "SQL" in described
        assert "命令拼接" in described
        assert "反序列化" in described

    def test_every_secret_pattern_named_in_description(self) -> None:
        described = describe_pattern_set()
        for name, _, _ in SECRET_PATTERNS:
            assert name in described, f"模式 {name} 未出现在描述里，零命中时读者无从知道查过它"

    def test_clean_file_yields_no_candidates(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path,
            "safe.py",
            'def go(uid):\n    cur.execute("SELECT 1 FROM t WHERE id = %s", (uid,))\n'
            "    return json.loads(data)\n",
        )
        assert found == []


class TestCandidateContract:
    def test_all_candidates_are_security_category(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "m.py", 'def go(p):\n    os.system("rm " + p)\n    return eval(p)\n'
        )
        assert found
        assert all(c.category is FindingCategory.SECURITY for c in found)

    def test_all_candidates_carry_evidence(self, tmp_path: Path) -> None:
        found = _ast_candidates(
            tmp_path, "m.py", 'def go(p):\n    os.system("rm " + p)\n    return pickle.loads(p)\n'
        )
        for candidate in found:
            assert candidate.detector_evidence
            assert candidate.snippet

    def test_order_is_deterministic(self, tmp_path: Path) -> None:
        body = 'def go(p):\n    os.system("rm " + p)\n    return eval(p)\n'
        first = _ast_candidates(tmp_path, "m.py", body)
        second = _ast_candidates(tmp_path, "m.py", body)
        assert [(c.line, c.kind) for c in first] == [(c.line, c.kind) for c in second]
