"""会话检查点：沙箱当前态的原子快照与重放校验。

事件流水是事实源，但每步都从快照全量重放太慢。检查点周期性地把「已落定事件数 +
沙箱状态指纹 + 时钟 tick」原子落盘，打开会话时：

* 检查点声明的事件前缀长度 = ``N``、指纹 = ``F``；
* 用事件流前 ``N`` 条在新沙箱里重放，若指纹 == ``F`` 则检查点可信，直接从第
  ``N+1`` 条续放；
* 若不一致（检查点撕裂、磁盘损坏），丢弃检查点，从快照全量重放——状态永远由
  不可变历史决定，检查点只是可重建的加速缓存。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

from .canonical import digest


@dataclass(frozen=True)
class Checkpoint:
    event_count: int
    tick: int
    clock_at: float
    state_fingerprint: str

    def as_dict(self) -> dict:
        return {
            "event_count": self.event_count,
            "tick": self.tick,
            "clock_at": self.clock_at,
            "state_fingerprint": self.state_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Checkpoint":
        return cls(
            event_count=int(payload["event_count"]),
            tick=int(payload["tick"]),
            clock_at=float(payload["clock_at"]),
            state_fingerprint=str(payload["state_fingerprint"]),
        )


class CheckpointStore:
    def __init__(self, session_dir: str) -> None:
        self._dir = os.path.abspath(session_dir)
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, "checkpoint.json")

    def path(self) -> str:
        return self._path

    def exists(self) -> bool:
        return os.path.isfile(self._path)

    def save(self, checkpoint: Checkpoint) -> None:
        payload = checkpoint.as_dict()
        payload["checksum"] = digest(payload)
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._dir, prefix=".ckpt.", suffix=".tmp", delete=False
        )
        try:
            with tmp as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp.name, self._path)
        except OSError:
            if os.path.exists(tmp.name):
                os.remove(tmp.name)
            raise

    def load(self) -> Checkpoint | None:
        if not os.path.isfile(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None  # 撕裂/损坏的检查点等同于没有
        checksum = payload.pop("checksum", None)
        try:
            cp = Checkpoint.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None
        if digest(cp.as_dict()) != checksum:
            return None
        return cp

    def clear(self) -> None:
        if os.path.isfile(self._path):
            os.remove(self._path)
