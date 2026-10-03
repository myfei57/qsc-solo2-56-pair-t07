"""从不可变事件流在新沙箱上确定性重放。

这是崩溃恢复、篡改校验、字节级复现三者共用的同一条代码路径：给定快照与事件列表，
装配一个全新的确定性沙箱，依序重放每一条 ``operator_action`` / ``sim_tick`` 事件，
得到沙箱状态指纹。因为没有任何随机源，重放结果逐字节稳定。

``session_begin``、``note``、``session_end`` 等事件不改变沙箱现场状态，重放时
只推进游标；它们仍在哈希链里接受完整性保护。
"""

from __future__ import annotations

from ..config import PlatformConfig
from .actions import execute
from .eventlog import Event
from .sandbox import build_sandbox, fingerprint_store
from .simulator import Simulator
from .snapshot import SnapshotStore


def replay_events(
    config: PlatformConfig,
    snapshots: SnapshotStore,
    snapshot_id: str,
    sandbox_root: str,
    events: list[Event],
    scenario,
    *,
    tick_seconds: float,
    up_to: int | None = None,
) -> dict:
    """在 ``sandbox_root`` 重建到第 ``up_to`` 条事件（默认全部）后的现场状态。"""

    import os
    import shutil

    if os.path.isdir(sandbox_root):
        shutil.rmtree(sandbox_root)
    os.makedirs(sandbox_root, exist_ok=True)
    from ..store.jsonfile import JsonStore

    target = JsonStore(sandbox_root)
    snapshots.restore_into(snapshot_id, target)

    start_at = float(events[0].payload.get("start_at", scenario.start_at)) if events else scenario.start_at
    platform = build_sandbox(config, sandbox_root, start_at)
    sim = Simulator(platform, scenario)

    chosen = events if up_to is None else events[:up_to]
    for event in chosen:
        if event.kind == "operator_action":
            execute(platform, event.payload["action"], dict(event.payload.get("args", {})))
        elif event.kind == "sim_tick":
            to_tick = int(event.payload.get("to_tick", event.sim_tick))
            sim.advance(to_tick)
        # begin / note / abort / end：不触碰沙箱现场。

    fp = fingerprint_store(platform.store)
    return {
        "sandbox_root": sandbox_root,
        "platform": platform,
        "simulator": sim,
        "tick": sim.tick,
        "clock_at": platform.clock.now(),
        "replayed": len(chosen),
        "fingerprint": fp["fingerprint"],
        "doc_count": fp["doc_count"],
    }
