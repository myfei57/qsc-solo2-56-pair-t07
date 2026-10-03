"""处置评估器：把哈希链里的动作流判成可解释的成绩单。

评估是纯函数：输入（题面、步骤定义、动作事件、注入零点），输出逐步骤判定 +
总分 + 关键违规。封存时算一次写进不可变报告；深度复核时用重放出来的事件再算
一次，两次不一致就说明报告或历史被动过。

判定口径（每一步都能在复盘里看到依据）：

- ``ok``        在时限内做了满足谓词的动作；
- ``late``      动作满足谓词，但超过该步时限（拿一半分，仍算处置成功）；
- ``wrong``     在时限内做了**同名动作但对象/结果不对**（如给别的门清故障）；
- ``missed``    到时限结束（或封存时）还没做对；
- 时限内做出的**非剧本动作**记为 :attr:`Evaluation.extras`（多做不乱罚，但
  关键违规除外）；
- 在"间隙可疑"状态下提前发车等**关键违规**单独列出（critical_violations），
  一旦发生直接不合格，不看总分。

评分：正确拿满 weight，迟误拿一半；满分 = Σweight。得分率换算成 0-100。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scenario import Scenario

VERDICT_OK = "ok"
VERDICT_LATE = "late"
VERDICT_WRONG = "wrong"
VERDICT_MISSED = "missed"


@dataclass
class StepVerdict:
    """一个标准步骤的判定。"""

    key: str
    title: str
    action: str
    deadline_s: float
    critical: bool
    weight: int
    verdict: str
    rationale: str
    matched_seq: int = 0
    matched_time: float = -1.0
    earned: float = 0.0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "action": self.action,
            "deadline_s": self.deadline_s,
            "critical": self.critical,
            "weight": self.weight,
            "verdict": self.verdict,
            "earned": self.earned,
            "matched_seq": self.matched_seq,
            "matched_time": self.matched_time,
            "rationale": self.rationale,
            "detail": self.detail,
        }


@dataclass
class Evaluation:
    """整场演练的成绩单。"""

    scenario: str
    steps: list[StepVerdict]
    extras: list[dict]
    critical_violations: list[dict]
    rejected_actions: list[dict]
    score: float
    max_score: int
    passed: bool
    pass_score: int
    finished_at: float

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "score": round(self.score, 1),
            "max_score": self.max_score,
            "pass_score": self.pass_score,
            "passed": self.passed,
            "finished_at": self.finished_at,
            "steps": [step.as_dict() for step in self.steps],
            "extras": self.extras,
            "critical_violations": self.critical_violations,
            "rejected_actions": self.rejected_actions,
        }


def evaluate(
    scenario: Scenario,
    problem: dict,
    action_events: list[dict],
    inject_time: float,
    *,
    end_time: float | None = None,
) -> Evaluation:
    """按剧本评估一组动作事件。

    ``action_events`` 为动作结果事件（含 seq/sim_time/request/result），
    ``inject_time`` 为故障零点，所有时限相对它计算。
    """

    verdicts = [
        StepVerdict(
            key=step.key,
            title=step.title,
            action=step.action,
            deadline_s=step.deadline_s,
            critical=step.critical,
            weight=step.weight,
            verdict=VERDICT_MISSED,
            rationale=step.rationale,
        )
        for step in scenario.steps
    ]
    by_action: dict[str, StepVerdict] = {v.action: v for v in verdicts}

    extras: list[dict] = []
    critical_violations: list[dict] = []
    rejected_actions: list[dict] = []
    last_time = inject_time

    for event in action_events:
        request = dict(event.get("request") or {})
        result = dict(event.get("result") or {})
        sim_time = float(event.get("sim_time", inject_time))
        last_time = max(last_time, sim_time)
        seq = int(event.get("seq", 0))
        action = str(request.get("action", ""))
        relative = sim_time - inject_time

        # 被现场拒绝的动作单独记录；关键动作的失败也可能构成违规。
        if result.get("ok") is False:
            rejected_actions.append(
                {"seq": seq, "action": action, "time": relative,
                 "violations": list(result.get("violations", []))}
            )

        verdict = by_action.get(action)

        # 关键违规检查对所有动作生效（含剧本动作）：不安全间隙下尝试发车，
        # 即使后面补做成功，这次违规尝试也要留在成绩单上。
        _check_critical_violation(action, request, result, relative, seq,
                                  critical_violations)

        if verdict is None:
            # 非剧本动作：记录但不扣分（多做不罚）。
            if action:
                extras.append({"seq": seq, "action": action, "time": relative})
            continue

        if verdict.verdict in (VERDICT_OK, VERDICT_LATE):
            # 已做对：后续同名动作不再重复计分。
            continue

        satisfied = scenario.step(verdict.key).matches(problem, request, result)
        if not satisfied:
            if verdict.verdict != VERDICT_WRONG:
                verdict.verdict = VERDICT_WRONG
                verdict.detail = {
                    "wrong_seq": seq,
                    "reason": "动作名正确但对象或结果不满足处置意图",
                }
            continue

        # 满足意图：按是否超时限判 ok / late。
        verdict.matched_seq = seq
        verdict.matched_time = relative
        if relative <= verdict.deadline_s:
            verdict.verdict = VERDICT_OK
            verdict.earned = float(verdict.weight)
        else:
            verdict.verdict = VERDICT_LATE
            verdict.earned = verdict.weight / 2.0
            verdict.detail = {"reason": f"超过时限 {verdict.deadline_s:.0f}s，迟误折半"}

    # 终盘：wrong 但最终仍没做对的，归入 missed（处置目标未达成）。
    earned_total = 0.0
    max_score = 0
    for verdict in verdicts:
        max_score += verdict.weight
        earned_total += verdict.earned
        if verdict.verdict == VERDICT_WRONG:
            verdict.verdict = VERDICT_MISSED
            verdict.earned = 0.0

    score = round(100.0 * earned_total / max_score, 1) if max_score else 0.0
    # 关键步骤未完成（missed/wrong）→ 关键违规
    for verdict in verdicts:
        if verdict.critical and verdict.verdict == VERDICT_MISSED:
            critical_violations.append(
                {"type": "critical_step_missed", "step": verdict.key,
                 "title": verdict.title, "action": verdict.action}
            )

    finished = last_time if end_time is None else end_time
    passed = score >= scenario.pass_score and not critical_violations
    return Evaluation(
        scenario=scenario.key,
        steps=verdicts,
        extras=extras,
        critical_violations=critical_violations,
        rejected_actions=rejected_actions,
        score=score,
        max_score=max_score,
        passed=passed,
        pass_score=scenario.pass_score,
        finished_at=finished - inject_time,
    )


def _check_critical_violation(
    action: str,
    request: dict,
    result: dict,
    relative: float,
    seq: int,
    sink: list[dict],
) -> None:
    """识别"做错方向"的关键违规：间隙未确认就发车。"""

    if action == "depart" and result.get("ok") is False:
        violations = list(result.get("violations", []))
        joined = " ".join(str(item) for item in violations)
        if "间隙" in joined or "gap" in joined.lower():
            sink.append(
                {"type": "depart_with_unsafe_gap", "seq": seq, "time": relative,
                 "reason": "间隙未确认安全即尝试发车，被联锁拒绝", "violations": violations}
            )


def next_recommendation(scenario: Scenario, evaluation: Evaluation) -> dict:
    """给出下一步标准动作提示（请求 hint 会进审计，提示不替值班员做决定）。"""

    for verdict in evaluation.steps:
        if verdict.verdict in (VERDICT_MISSED, VERDICT_WRONG):
            step = scenario.step(verdict.key)
            return {
                "step": verdict.key,
                "title": verdict.title,
                "action": verdict.action,
                "deadline_s": verdict.deadline_s,
                "rationale": step.rationale if step else verdict.rationale,
            }
    return {"step": "", "title": "标准步骤已全部完成", "action": "", "deadline_s": 0.0,
            "rationale": ""}
