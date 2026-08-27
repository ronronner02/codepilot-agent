"""分析历史的落盘与启动重建（U21，R-43~R-46、KTD5）。

**存 API 响应形状，不 pickle 内部 dataclass。** 落盘文件的读者是下一次进程启动，而内部类型会
随实现演进——pickle 的内部对象在类改名或字段增删后就读不回来了，而那时数据已经在磁盘上。
`ResultResponse` 是对外契约，演进受 additive 约束。

**带 `schema_version` 整数字段。** 版本不认识时跳过该条并计入损坏计数（R-44 已要求单文件损坏
跳过，版本不匹配走同一条路径）。没有版本字段的话，将来一次不兼容的格式变更会让全部历史变成
「解析失败」，而那与「文件损坏」在日志里不可区分。

**凭证不在落盘内容中**（NA-02）：`ResultResponse` 里本来就没有凭证字段，这是结构性排除而非
「记得删」。

**先写临时文件再原子重命名。** 进程在写入中途被杀会留下半个 JSON——扫描逻辑虽然能跳过它，
但那会让每次启动都多一条损坏计数，而那条计数是永久的假警报。

落盘目录不被配额清理触及（KTD10）：清理只删仓库副本，所以被清理的分析仍能从列表载入，只是
查看器与检索读不到源文件。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import time

from backend.api.schemas import ResultResponse

logger = logging.getLogger("codepilot.history")

# 落盘格式的版本。
#
# 递增的时机是**不兼容**变更（删字段、改字段语义）。加字段不必递增——读取端对未知字段宽容，
# 而旧文件缺新字段时 pydantic 的默认值会补上。
SCHEMA_VERSION = 1

# 内存索引的条数上限。
#
# 落盘文件不受它约束——磁盘上的历史保留策略被 KTD10 推到「实际磁盘压力出现时再定」。这道
# 上限管的是另一件事：内存中的索引列表每次分析都增长，而它整份进列表端点的响应。没有上界
# 的话，一个长期运行的部署会同时得到内存增长与响应体膨胀，而两者都不产生错误、只是越来越慢。
#
# 取 200：按每小时 3 次的限流，那是几十天的量，足够「最近分析」这个用途。更早的条目仍在
# 磁盘上，只是不进这一份索引——需要时按 task_id 直接读文件仍可得。
MAX_INDEXED_ENTRIES = 200


@dataclass(frozen=True)
class HistoryEntry:
    """一条历史记录。列表端点从它派生摘要。"""

    result: ResultResponse
    created_at: float
    schema_version: int = SCHEMA_VERSION


@dataclass
class ScanOutcome:
    """一次目录扫描的结果。

    `corrupt_count` 单独给出而非只记日志：`/api/health` 与排查都要用它，而从日志里数行数
    不是一条可查询的路径。
    """

    entries: list[HistoryEntry]
    corrupt_count: int = 0


def _path_for(analyses_dir: Path, task_id: str) -> Path:
    # task_id 是十六进制的 uuid 片段，不含路径分隔符——但仍过一次 basename，
    # 不让「文件名来自请求参数」这件事在将来变成路径穿越。
    safe = Path(task_id).name
    return analyses_dir / f"{safe}.json"


def save(analyses_dir: Path, result: ResultResponse, created_at: float | None = None) -> bool:
    """落盘一次分析结果。返回是否成功。

    失败只记 warning 不抛：持久化失败不该让一次已经完成的分析变成失败（那会丢掉真正的
    产出）。返回值让调用方能决定是否要在别处提示。
    """
    try:
        analyses_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("无法创建落盘目录 %s：%s", analyses_dir, exc)
        return False

    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at if created_at is not None else time(),
        # model_dump 出的是 API 形状；凭证字段不在其中（NA-02 的结构性排除）。
        "result": result.model_dump(),
    }

    target = _path_for(analyses_dir, result.task_id)
    tmp_path: str | None = None
    try:
        # 同目录建临时文件：跨设备的 rename 不是原子的，而 tempfile 的默认目录可能在别的卷上。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=analyses_dir,
            prefix=f".{target.stem}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = handle.name
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            # fsync 后再 rename：只 flush 的话数据可能还在 OS 缓存里，断电后留下一个
            # 大小正确但内容为空的文件——那比没有文件更难排查。
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("落盘 %s 失败：%s", result.task_id, exc)
        return False
    finally:
        if tmp_path is not None:
            # 重命名没走到，清掉半成品。留着它会在下次扫描时被当成损坏文件计数。
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_one(path: Path) -> HistoryEntry | None:
    """读一个落盘文件。不可用时返回 None（由调用方计入损坏计数）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("跳过无法解析的历史文件 %s：%s", path.name, exc)
        return None

    if not isinstance(raw, dict):
        logger.info("跳过结构异常的历史文件 %s", path.name)
        return None

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        # 版本不认识与解析失败走同一条路径（R-44）。不尝试迁移：迁移逻辑要为每个历史版本
        # 写一遍，而这些数据的价值不足以支撑那份维护成本。
        logger.info(
            "跳过版本不匹配的历史文件 %s（文件 %r，当前 %d）",
            path.name,
            version,
            SCHEMA_VERSION,
        )
        return None

    try:
        result = ResultResponse.model_validate(raw.get("result"))
    except Exception as exc:  # noqa: BLE001 — pydantic 的校验错误类型不止一种
        logger.info("跳过字段缺失或非法的历史文件 %s：%s", path.name, exc)
        return None

    created_at = raw.get("created_at")
    return HistoryEntry(
        result=result,
        created_at=float(created_at) if isinstance(created_at, (int, float)) else 0.0,
    )


def scan(analyses_dir: Path) -> ScanOutcome:
    """扫描落盘目录，重建历史列表。单文件损坏跳过并计数（R-44）。"""
    if not analyses_dir.is_dir():
        return ScanOutcome(entries=[])

    entries: list[HistoryEntry] = []
    corrupt = 0
    try:
        candidates = sorted(analyses_dir.glob("*.json"))
    except OSError as exc:
        logger.warning("无法列出落盘目录 %s：%s", analyses_dir, exc)
        return ScanOutcome(entries=[])

    for path in candidates:
        entry = load_one(path)
        if entry is None:
            corrupt += 1
            continue
        entries.append(entry)

    # 时间倒序：最新的在最前（R-45）。
    entries.sort(key=lambda e: e.created_at, reverse=True)
    # 截断在排序**之后**：先排序才能保证留下的是最近的那些。截断在前会留下任意一批。
    if len(entries) > MAX_INDEXED_ENTRIES:
        logger.info(
            "落盘历史有 %d 条，索引只保留最近 %d 条",
            len(entries),
            MAX_INDEXED_ENTRIES,
        )
        del entries[MAX_INDEXED_ENTRIES:]
    if corrupt:
        logger.warning(
            "历史目录中有 %d 个文件无法载入，已跳过（共 %d 个候选）",
            corrupt,
            len(candidates),
        )
    logger.info("载入 %d 条历史分析", len(entries))
    return ScanOutcome(entries=entries, corrupt_count=corrupt)
