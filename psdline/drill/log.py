"""演练事件日志：哈希链、只追加、崩溃可恢复、终态只读。

每个会话一条 ``events.jsonl``。每行一个信封记录：

    {
      "seq": 3, "sim_time": 12.0, "kind": "action",
      "actor": "dispatcher:zhangsan", "payload": {...},
      "wall_at": "2026-10-03T05:00:00Z",
      "prev_hash": "…", "hash": "…"
    }

``hash = sha256(规范化(seq, sim_time, kind, actor, payload, prev_hash))``。
``wall_at`` 只是墙上时钟注释，刻意**不**进哈希：重放时语义记录必须逐字节
可复现，而墙上时间天然不同。篡改任何一个被哈希覆盖的字段、删行或调序，从该
行起链全部断裂，:meth:`verify` 会精确指出断点序号。

崩溃安全：追加是"单行 write + flush + fsync"。下次打开时若末行是撕裂的半行
（掉电瞬间常见），只截掉这一条无法解析的末行——已 fsync 的完整记录一条不丢。

终态后把文件权限改成只读，是哈希之外的第二道防线（纵深防御）。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

from ..clock import Clock
from .crypto import GENESIS_HASH, canonical_dumps, chain_hash
from .errors import LogIntegrityError

EVENTS_FILE = "events.jsonl"

#: 结果比对时忽略的非确定性字段（含 uuid4 生成的现场操作/审计标识）。
NONDETERMINISTIC_KEYS = frozenset({"op_id", "audit_id", "arrival_audit"})


@dataclass(frozen=True)
class EventRecord:
    """一条哈希链事件。"""

    seq: int
    sim_time: float
    kind: str
    actor: str
    payload: dict
    wall_at: str
    prev_hash: str
    hash: str

    def as_line(self) -> str:
        return canonical_dumps(self.__dict__)

    @classmethod
    def from_dict(cls, raw: dict) -> "EventRecord":
        return cls(
            seq=int(raw["seq"]),
            sim_time=float(raw["sim_time"]),
            kind=str(raw["kind"]),
            actor=str(raw.get("actor", "")),
            payload=dict(raw.get("payload") or {}),
            wall_at=str(raw.get("wall_at", "")),
            prev_hash=str(raw.get("prev_hash", GENESIS_HASH)),
            hash=str(raw.get("hash", "")),
        )


@dataclass(frozen=True)
class ChainBreak:
    """哈希链上的第一个断点。"""

    seq: int
    reason: str
    expected: str = ""
    actual: str = ""


class DrillEventLog:
    """会话级只追加哈希链日志。"""

    def __init__(self, session_dir: str, clock: Clock) -> None:
        self._dir = os.path.abspath(session_dir)
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, EVENTS_FILE)
        self._clock = clock
        self._lock = threading.RLock()
        self._records: list[EventRecord] = []
        self._tip = GENESIS_HASH
        self._sealed = False
        self._open()

    @property
    def path(self) -> str:
        return self._path

    @property
    def tip_hash(self) -> str:
        with self._lock:
            return self._tip

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def records(self) -> list[EventRecord]:
        with self._lock:
            return list(self._records)

    # ---- 追加 ---------------------------------------------------------

    def append(self, kind: str, payload: dict, actor: str = "") -> EventRecord:
        """追加一条事件并 fsync，返回落盘记录。

        被哈希覆盖的只有 (seq, sim_time, kind, actor, payload, prev_hash)；
        ``wall_at`` 不进链，仅作注释。
        """

        with self._lock:
            if self._sealed:
                raise LogIntegrityError("事件日志已封存只读，拒绝追加", component="drill-log")
            if os.path.exists(self._path) and not os.access(self._path, os.W_OK):
                raise LogIntegrityError("事件日志文件为只读，拒绝追加", component="drill-log")
            seq = len(self._records) + 1
            sim_time = float(self._clock.now())
            fields = ("seq", "sim_time", "kind", "actor", "payload")
            body = {
                "seq": seq,
                "sim_time": sim_time,
                "kind": kind,
                "actor": actor,
                "payload": payload,
                "prev_hash": self._tip,
            }
            # 只对五个语义字段做链哈希；prev_hash 由 chain_hash 作为外层链接入。
            digest = chain_hash(self._tip, {key: body[key] for key in fields})
            record = EventRecord(
                seq=seq,
                sim_time=sim_time,
                kind=kind,
                actor=actor,
                payload=payload,
                wall_at=self._clock.utc_iso(),
                prev_hash=self._tip,
                hash=digest,
            )
            line = record.as_line() + "\n"
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._records.append(record)
            self._tip = digest
            return record

    def freeze(self) -> None:
        """终态封存：内存标记 + 文件降为只读（追加会被操作系统拒绝）。"""

        with self._lock:
            self._sealed = True
            if os.path.exists(self._path):
                os.chmod(self._path, 0o444)

    # ---- 校验 ---------------------------------------------------------

    def verify(self) -> ChainBreak | None:
        """从首条重算哈希链，返回第一个断点；完整返回 ``None``。"""

        with self._lock:
            previous = GENESIS_HASH
            for record in self._records:
                if record.prev_hash != previous:
                    return ChainBreak(record.seq, "prev_hash_mismatch", previous, record.prev_hash)
                expected = chain_hash(
                    previous,
                    {k: getattr(record, k) for k in
                     ("seq", "sim_time", "kind", "actor", "payload")},
                )
                if expected != record.hash:
                    return ChainBreak(record.seq, "hash_mismatch", expected, record.hash)
                previous = record.hash
            return None

    def assert_intact(self) -> None:
        """校验不通过直接抛 :class:`LogIntegrityError`。"""

        broken = self.verify()
        if broken is not None:
            raise LogIntegrityError(
                f"演练历史哈希链在第 {broken.seq} 条断裂（{broken.reason}），"
                "历史不可信，禁止继续使用",
                component="drill-log",
            )

    # ---- 打开与撕裂恢复 ----------------------------------------------

    def _open(self) -> None:
        """读入既有日志：重算链、识别撕裂末行。

        末行 JSON 无法解析时视为掉电撕裂，截掉它（它从未被 fsync 成完整行）；
        哈希断裂不属于撕裂，绝不静默处理，直接抛错。
        """

        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        records: list[EventRecord] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                records.append(EventRecord.from_dict(raw))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                if index == len(lines):
                    # 末行撕裂：截掉重写，已确认 fsync 的前缀不动。
                    self._truncate(len(lines) - 1)
                    lines = lines[:-1]
                    break
                raise LogIntegrityError(
                    f"第 {index} 行事件损坏（非末行，不可能是撕裂）",
                    component="drill-log",
                )
        # 重算链，不采信文件里自称的 prev/hash。
        previous = GENESIS_HASH
        for record in records:
            expected = chain_hash(
                previous,
                {k: getattr(record, k) for k in
                 ("seq", "sim_time", "kind", "actor", "payload")},
            )
            if record.prev_hash != previous or record.hash != expected:
                raise LogIntegrityError(
                    f"第 {record.seq} 条事件哈希链断裂，演练历史可能被篡改",
                    component="drill-log",
                )
            previous = record.hash
        self._records = records
        self._tip = previous
        if not os.access(self._path, os.W_OK):
            self._sealed = True

    def _truncate(self, keep_lines: int) -> None:
        with open(self._path, "w", encoding="utf-8") as handle:
            handle.writelines(self._raw_lines()[:keep_lines])
            handle.flush()
            os.fsync(handle.fileno())

    def _raw_lines(self) -> list[str]:
        if not os.path.exists(self._path):
            return []
        with open(self._path, "r", encoding="utf-8") as handle:
            return handle.readlines()


def normalize_result(result: dict) -> dict:
    """剥掉执行结果里的非确定性字段，供重放逐字节比对。"""

    return {
        key: value
        for key, value in result.items()
        if key not in NONDETERMINISTIC_KEYS
    }


def iter_paired(records: list[EventRecord]) -> list[tuple[dict, dict | None]]:
    """把 request/result 成对事件按 request_id 配对。

    返回 ``(request, result_or_None)``。result 为 ``None`` 即"执行中崩溃"：
    意图已落盘、结果没落盘——恢复策略由服务层决定（fail-closed，不自动重试）。
    """

    requests: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        if record.kind not in ("action", "injection"):
            continue
        payload = record.payload
        if payload.get("phase") == "request":
            request_id = str(payload.get("request_id", ""))
            requests[request_id] = {"record": record, **payload}
            order.append(request_id)
    results: dict[str, dict] = {}
    for record in records:
        if record.kind not in ("action", "injection"):
            continue
        payload = record.payload
        if payload.get("phase") == "result":
            results[str(payload.get("request_id", ""))] = {"record": record, **payload}
    return [(requests[rid], results.get(rid)) for rid in order]
