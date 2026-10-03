"""确定性模拟器：把场景事件施加到沙箱并推进演练时钟。

模拟器不发明任何业务规则：故障注入直接调用沙箱 ``Platform.set_fault``（与现场
值班员在维修模式下登记故障走同一套联锁路径，会真实锁门），恢复条件只是“设备在
第 N 步具备排故条件”，真正的 ``clear_fault`` 仍要值班员按规则操作。每个演练
tick 推进固定秒数并跑一次真实控制周期 ``cycle``。

所有时钟推进用 :class:`ManualClock`，无随机数、无 wall-clock 进入沙箱状态，
因此同一场景、同一动作序列逐字节可复现。
"""

from __future__ import annotations

from ..app import Platform
from .sandbox import sandbox_clock
from .scenario import Scenario, ScenarioEvent


class Simulator:
    """在沙箱平台上按步重放场景外部事件。"""

    def __init__(self, platform: Platform, scenario: Scenario) -> None:
        self._platform = platform
        self._scenario = scenario
        self._tick = 0
        self._applied: list[int] = []

    @property
    def tick(self) -> int:
        return self._tick

    def inject(self, event: ScenarioEvent) -> dict:
        """施加单个场景事件，返回沙箱内真实产生的结果。"""

        result: dict = {"event_kind": event.kind, "target": event.target, "detail": dict(event.detail)}
        if event.kind == "inject_fault":
            outcome = self._platform.set_fault(event.target)
            result["applied"] = "set_fault"
            result["outcome"] = _safe(outcome)
        elif event.kind == "restore_fault":
            # 设备物理恢复不等于联锁解锁：仅登记“已具备排故条件”，门锁保持，
            # 等待值班员按 releasable(门关闭+间隙无异常) 执行 clear_fault。
            result["applied"] = "note_ready_to_clear"
            result["note"] = "设备已恢复，门仍锁，等待值班员 clear_fault"
        elif event.kind in ("crowd_alarm", "info"):
            result["applied"] = "note"
        else:
            result["applied"] = "ignored_unknown_event"
        return result

    def advance(self, to_tick: int) -> list[dict]:
        """把演练时钟从当前 tick 推进到 ``to_tick``（含），施加途中到期事件。"""

        effects: list[dict] = []
        clock = sandbox_clock(self._platform)
        while self._tick < to_tick:
            self._tick += 1
            clock.advance(self._scenario.tick_seconds)
            due = self._scenario.events_due(self._tick)
            injected = [self.inject(event) for event in due]
            cycle = self._platform.cycle(dt=self._scenario.tick_seconds)
            effects.append(
                {
                    "tick": self._tick,
                    "clock_at": clock.now(),
                    "injected": injected,
                    "cycle": {
                        "cycle": cycle.get("cycle"),
                        "locked_count": cycle.get("locked_count"),
                        "stale_positions": cycle.get("stale_positions"),
                        "violations": cycle.get("violations", []),
                    },
                }
            )
            self._applied.append(self._tick)
        return effects


def _safe(report: dict) -> dict:
    """只保留可序列化、复盘关心的字段。"""

    keys = ("ok", "psd_id", "locked", "violations")
    return {key: report.get(key) for key in keys if key in report}
