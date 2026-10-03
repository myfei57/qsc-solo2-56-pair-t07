"""演练会话事件流水：只追加、哈希成链、撕裂可裁、篡改可验。

每个演练会话有一条独立的只追加事件流（``events.jsonl``）。事件类型包括：

* ``session_begin``  会话建立（含快照编号、场景、操作者、策略版本）；
* ``operator_action`` 值班员的一步处置（含门控判定、判断依据、沙箱结果）；
* ``sim_tick``       演练时钟推进与场景注入（故障、恢复、客流等）；
* ``note``           过程备注（广播、联络等不改变现场状态的动作）；
* ``session_abort`` / ``session_end`` 中止或正常结束。

每条事件携带 ``seq``（从 0 起）与 ``prev_hash``（上一条规范化字节的 SHA-256），
形成逐条哈希链。因此历史既不可被插入/删除（链会断）、也不可被改写（哈希会变），
重放时逐链核对即可发现任何越权篡改。

崩溃安全：追加采用「写临时行 + fsync + 不追加换行的最后一行」策略，并在打开时
裁掉结尾不完整或哈希对不上的“撕裂尾”——被裁事件视为未落定，值班员可凭
``client_request_id`` 安全重试（演练动作没有现场副作用，重放幂等）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

from ..errors import RailError
from .canonical import digest

EVENT_VERSION = 1
TAIL_MARK = "\n"  # 每条完整记录以换行结尾；最后一行无换行者视为撕裂。


class EventLogError(RailError):
    """事件流水读写或校验失败。"""


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    at: float
    sim_tick: int
    payload: dict
    prev_hash: str
    this_hash: str

    def as_dict(self) -> dict:
        return {
            "version": EVENT_VERSION,
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "sim_tick": self.sim_tick,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "this_hash": self.this_hash,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Event":
        return cls(
            seq=int(raw["seq"]),
            kind=str(raw["kind"]),
            at=float(raw.get("at", 0.0)),
            sim_tick=int(raw.get("sim_tick", 0)),
            payload=dict(raw.get("payload", {})),
            prev_hash=str(raw.get("prev_hash", "")),
            this_hash=str(raw.get("this_hash", "")),
        )


def event_hash(seq: int, kind: str, at: float, sim_tick: int, payload: dict, prev_hash: str) -> str:
    body = {
        "version": EVENT_VERSION,
        "seq": seq,
        "kind": kind,
        "at": at,
        "sim_tick": sim_tick,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return digest(body)


class ChainedEventLog:
    """绑定单个会话目录的哈希链只追加流水。"""

    def __init__(self, session_dir: str) -> None:
        self._dir = os.path.abspath(session_dir)
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, "events.jsonl")
        self._events: list[Event] = []
        self._truncated: list[dict] = []
        self._load()

    @property
    def path(self) -> str:
        return self._path

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    @property
    def truncated_tail(self) -> list[dict]:
        """打开时裁掉的撕裂/失配尾记录（供恢复审计）。"""

        return list(self._truncated)

    @property
    def next_seq(self) -> int:
        return len(self._events)

    @property
    def head_hash(self) -> str:
        return self._events[-1].this_hash if self._events else ""

    def append(self, kind: str, at: float, sim_tick: int, payload: dict) -> Event:
        seq = self.next_seq
        prev_hash = self.head_hash
        this_hash = event_hash(seq, kind, at, sim_tick, payload, prev_hash)
        event = Event(seq, kind, at, sim_tick, payload, prev_hash, this_hash)
        self._durably_append(event)
        self._events.append(event)
        return event

    def _durably_append(self, event: Event) -> None:
        line = json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True)
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + TAIL_MARK)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise EventLogError(f"追加演练事件失败: {error}", component="drill.eventlog") from error

    def _load(self) -> None:
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "rb") as handle:
                data = handle.read()
        except OSError as error:
            raise EventLogError(f"读取演练事件流失败: {error}", component="drill.eventlog") from error

        text = data.decode("utf-8")
        lines = text.split(TAIL_MARK)
        # split 末尾：若文件以换行结尾，最后一个元素是空串；否则是“无换行”的撕裂尾。
        dangling = ""
        if lines and lines[-1] != "":
            dangling = lines[-1]
        complete = [line for line in lines[:-1] if line.strip()] if lines else []
        if dangling.strip():
            self._truncated.append({"reason": "unterminated_tail", "raw": dangling[:200]})

        good: list[Event] = []
        for line in complete:
            try:
                raw = json.loads(line)
                event = Event.from_dict(raw)
            except (ValueError, KeyError, TypeError):
                self._truncated.append({"reason": "unparseable_line", "raw": line[:200]})
                break
            prev = good[-1].this_hash if good else ""
            if event.seq != len(good) or event.prev_hash != prev:
                self._truncated.append({"reason": "broken_chain", "seq": event.seq})
                break
            recomputed = event_hash(
                event.seq, event.kind, event.at, event.sim_tick, event.payload, event.prev_hash
            )
            if recomputed != event.this_hash:
                self._truncated.append({"reason": "hash_mismatch", "seq": event.seq})
                break
            good.append(event)

        if len(good) != len(complete):
            # 有完整但失配的记录：物理裁掉从失配点开始的所有字节，回到最后可信前缀。
            self._rewrite_to_prefix(good)
        elif dangling.strip():
            # 只剩撕裂尾：把它从文件尾部去掉。
            self._rewrite_to_prefix(good)
        self._events = good

    def _rewrite_to_prefix(self, good: list[Event]) -> None:
        directory = self._dir
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".events.", suffix=".tmp", delete=False
        )
        try:
            with tmp as handle:
                for event in good:
                    handle.write(json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True))
                    handle.write(TAIL_MARK)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp.name, self._path)
        except OSError as error:
            raise EventLogError(f"修复演练事件流失败: {error}", component="drill.eventlog") from error

    def verify_chain(self) -> dict:
        """从头逐链复核，返回链顶哈希与是否完整。"""

        result = {"ok": True, "count": len(self._events), "first_bad_seq": None, "head_hash": self.head_hash}
        prev = ""
        for index, event in enumerate(self._events):
            recomputed = event_hash(
                event.seq, event.kind, event.at, event.sim_tick, event.payload, event.prev_hash
            )
            if event.seq != index or event.prev_hash != prev or recomputed != event.this_hash:
                result["ok"] = False
                result["first_bad_seq"] = event.seq
                return result
            prev = event.this_hash
        return result
