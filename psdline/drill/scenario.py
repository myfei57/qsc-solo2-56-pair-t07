"""演练场景定义。

场景描述「演练时钟走到第几步，现场会发生什么」（故障注入、故障恢复、客流告警
等外部事件），以及这一科目的评分清单（值班员应当做对哪些处置、哪些动作在解除前
不允许发生）。场景是**纯数据**、按步触发、与随机数无关，因此同一场景重放完全
一致。

内置一个最贴合痛点的科目：``morning_peak_psd``（早高峰站台门故障应急处置）。
值班员练的正是“设备不空、早高峰不能真操作”的那套处置流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScenarioEvent:
    """在演练时钟第 ``at_tick`` 步注入的一个外部事件。"""

    at_tick: int
    kind: str                       # inject_fault / restore_fault / crowd_alarm / info
    target: str = ""
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChecklistItem:
    """一条评分要点。"""

    code: str
    text: str
    expect: str                      # must_do / must_not_do_before / must_ack
    action: str = ""
    reference: str = ""              # 对应的规则码/注入事件
    weight: int = 1


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    brief: str
    tick_seconds: float
    start_at: float
    events: tuple[ScenarioEvent, ...]
    checklist: tuple[ChecklistItem, ...]
    stations: tuple[str, ...]

    def events_due(self, tick: int) -> list[ScenarioEvent]:
        return [event for event in self.events if event.at_tick == tick]


def morning_peak_psd() -> Scenario:
    """早高峰站台门故障应急演练科目。

    背景：早高峰，ST1 上行站台门 G01-01 在第 2 步发生门区故障并被联锁锁定；
    第 18 步现场（模拟）完成检修恢复。值班员要在不动真设备的前提下，按真实
    联锁规则完成“确认—联控—排故—解锁—恢复行车”的处置，并保证故障未解除前
    不让相关列车带隐患发车。
    """

    events = (
        ScenarioEvent(2, "inject_fault", "ST1-G01-01", {"name": "站台门 G01-01 门区故障"}),
        ScenarioEvent(3, "crowd_alarm", "ST1", {"level": "high", "note": "早高峰客流积压告警"}),
        ScenarioEvent(18, "restore_fault", "ST1-G01-01", {"note": "现场检修完成，设备恢复可排故"}),
    )
    checklist = (
        ChecklistItem("C1", "发现故障后核对门区与联锁锁定状态", "must_ack", reference="inject_fault:2"),
        ChecklistItem("C2", "对涉事站台门执行门区清扫/确认关门到位", "must_do", action="sweep", reference="ST1"),
        ChecklistItem("C3", "确认列车间隙无异常", "must_do", action="check_gap", reference="GAP-CLEAR-BEFORE-DEPART"),
        ChecklistItem("C4", "恢复条件具备后清除联锁故障并解锁", "must_do", action="clear_fault", reference="INTERLOCK-RELEASE-GATE-CLOSED-GAP-CLEAR"),
        ChecklistItem("C5", "故障未解除前不得带隐患放行发车", "must_not_do_before", action="depart", reference="inject_fault:2"),
        ChecklistItem("C6", "处置过程符合真实联锁（无强行解锁/越序）", "must_ack", reference="no-violations"),
    )
    return Scenario(
        key="morning_peak_psd",
        title="早高峰站台门故障应急处置",
        brief="ST1 上行站台门早高峰门区故障，按联锁规则完成确认、排故、解锁与恢复行车。",
        tick_seconds=30.0,
        start_at=1_758_000_000.0,
        events=events,
        checklist=checklist,
        stations=("ST1", "ST2", "ST3"),
    )


SCENARIOS = {morning_peak_psd().key: morning_peak_psd}


def get_scenario(key: str) -> Scenario:
    if key not in SCENARIOS:
        raise KeyError(f"未知演练场景: {key}")
    return SCENARIOS[key]()
