"""确定性演练沙箱：用真实 Platform 代码，但时钟与 ID 全部可复现。

演练沙箱直接装配真实的 :class:`~psdline.app.Platform`（真实的联锁、门控、间隙、
配额、审计调用链），唯一区别是：

1. 存储根是快照复制出的**独立目录**，与现场存储根物理分离；
2. 时钟用 :class:`~psdline.clock.ManualClock`，从快照捕获时刻起步，演练按步推进；
3. 把 :class:`~psdline.ids.IdFactory` 的随机 UUID 换成「计数器 + 前缀」的确定
   标识。现场用随机 ID 没问题，但演练要保证“同样输入重放得到同样结果”，任何随机
   字节进入审计 ``op_id`` 都会破坏这一点。

这样值班员每一步触发的都是**和现场一模一样的业务判定逻辑**，却不会触碰现场。
"""

from __future__ import annotations

import json
import os

from ..app import Platform
from ..clock import ManualClock
from ..config import PlatformConfig, with_storage_root
from ..ids import IdFactory
from .canonical import digest


class DeterministicIds(IdFactory):
    """计数器式标识：``<prefix>-<序号:012d>``，重放稳定。"""

    def __init__(self, prefix: str) -> None:
        super().__init__(prefix)
        self._seq = 0

    def next(self) -> str:
        self._issued += 1
        self._seq += 1
        return f"{self._prefix}-{self._seq:012d}"


def build_sandbox(config: PlatformConfig, sandbox_root: str, start_at: float) -> Platform:
    """在独立目录里装配一个确定性、与现场同构的演练平台。"""

    sandbox_config = with_storage_root(config, sandbox_root)
    clock = ManualClock(start=start_at)
    platform = Platform(config=sandbox_config, clock=clock)
    # 让现场逻辑里两个发号器都确定化（操作号、审计号）。
    platform.ids = DeterministicIds("op")
    platform.audit._ids = DeterministicIds("audit")  # noqa: SLF001 - 同构沙箱装配
    return platform


def sandbox_clock(platform: Platform) -> ManualClock:
    clock = platform.clock
    if not isinstance(clock, ManualClock):
        raise TypeError("演练沙箱时钟必须是 ManualClock")
    return clock


def fingerprint_store(store) -> dict:
    """对一个 JsonStore 的全部当前态文档算只读指纹。

    用于演练前后核对“现场存储一个字节都没变”。只读取、不写入、不加锁。
    """

    docs = {}
    for doc_id in store.list_ids():
        path = store.path_of(doc_id)
        with open(path, "rb") as handle:
            docs[doc_id] = digest(json.loads(handle.read().decode("utf-8")))
    return {
        "root": os.path.abspath(store.root),
        "doc_count": len(docs),
        "fingerprint": digest({"docs": {k: docs[k] for k in sorted(docs)}}),
    }
