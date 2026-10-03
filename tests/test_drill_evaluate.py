"""评估器与剧本：对错判据、时限、关键违规、打分口径。"""

from __future__ import annotations

import pytest

from psdline.drill.evaluate import (
    VERDICT_LATE,
    VERDICT_MISSED,
    VERDICT_OK,
    VERDICT_WRONG,
    evaluate,
    next_recommendation,
)
from psdline.drill.scenarios import SCENARIOS, gap_alarm, psd_fault, scenario_catalog, stale_position


def _result_event(seq, sim_time, action, params, ok=True, violations=None, extra=None):
    return {
        "seq": seq,
        "sim_time": sim_time,
        "request": {"action": action, **params},
        "result": {"ok": ok, "action": action, "violations": violations or [],
                   **(extra or {})},
    }


def test_catalog_has_three_scenarios_with_real_objects():
    catalog = scenario_catalog()
    keys = {scenario["key"] for scenario in catalog}
    assert keys == {"psd_fault", "gap_alarm", "stale_position"}
    for scenario in SCENARIOS.values():
        assert scenario.steps  # 每个剧本都有标准步骤
        assert 0 < scenario.pass_score <= 100
    for item in catalog:
        assert item["steps"]
        assert all(step["rationale"] for step in item["steps"])  # 每步都有判断依据


def test_correct_on_time_sequence_scores_full():
    problem = dict(psd_fault.target, psd_id="ST1-G01-01")
    events = [
        _result_event(1, 0.0, "fault_status", {"psd_id": "ST1-G01-01"}),
        _result_event(2, 10.0, "sweep", {"station_id": "ST1"}),
        _result_event(3, 20.0, "inspect_gate", {"psd_id": "ST1-G01-01"}),
        _result_event(4, 30.0, "clear_fault", {"psd_id": "ST1-G01-01"}),
        _result_event(5, 40.0, "depart", {"train_id": "T01", "station_id": "ST1"}),
    ]
    evaluation = evaluate(psd_fault, problem, events, inject_time=0.0)
    assert evaluation.score == 100.0
    assert evaluation.passed is True
    assert {step.verdict for step in evaluation.steps} == {VERDICT_OK}


def test_wrong_target_gate_is_caught_by_predicate():
    """动作名对、对象错（给别的门清故障）必须判错。"""

    problem = dict(psd_fault.target, psd_id="ST1-G01-01")
    events = [
        _result_event(1, 0.0, "clear_fault", {"psd_id": "ST2-G01-01"}),
    ]
    evaluation = evaluate(psd_fault, problem, events, inject_time=0.0)
    clear = next(step for step in evaluation.steps if step.key == "clear")
    assert clear.verdict == VERDICT_MISSED  # 终盘 wrong 归并为 missed
    assert any(v["type"] == "critical_step_missed" and v["step"] == "clear"
               for v in evaluation.critical_violations)
    assert evaluation.passed is False


def test_late_action_earns_half_and_still_completes():
    problem = dict(psd_fault.target, psd_id="ST1-G01-01")
    events = [
        _result_event(1, 0.0, "fault_status", {"psd_id": "ST1-G01-01"}),
        _result_event(2, 70.0, "sweep", {"station_id": "ST1"}),   # 晚于 60s
        _result_event(3, 80.0, "inspect_gate", {"psd_id": "ST1-G01-01"}),
        _result_event(4, 90.0, "clear_fault", {"psd_id": "ST1-G01-01"}),
        _result_event(5, 100.0, "depart", {"train_id": "T01", "station_id": "ST1"}),
    ]
    evaluation = evaluate(psd_fault, problem, events, inject_time=0.0)
    sweep = next(step for step in evaluation.steps if step.key == "sweep")
    assert sweep.verdict == VERDICT_LATE
    assert sweep.earned == 1.0  # weight 2 的一半
    assert evaluation.score < 100.0
    assert evaluation.passed is True  # 关键步都完成，仍合格但分数低


def test_missed_critical_step_fails_regardless_of_score():
    problem = dict(psd_fault.target, psd_id="ST1-G01-01")
    # 只做前四步，漏掉最后的关键发车
    events = [
        _result_event(1, 0.0, "fault_status", {"psd_id": "ST1-G01-01"}),
        _result_event(2, 10.0, "sweep", {"station_id": "ST1"}),
        _result_event(3, 20.0, "inspect_gate", {"psd_id": "ST1-G01-01"}),
        _result_event(4, 30.0, "clear_fault", {"psd_id": "ST1-G01-01"}),
    ]
    evaluation = evaluate(psd_fault, problem, events, inject_time=0.0)
    assert any(v["step"] == "depart" for v in evaluation.critical_violations)
    assert evaluation.passed is False


def test_unsafe_gap_departure_is_critical_violation():
    problem = dict(gap_alarm.target)
    events = [
        # 间隙未确认就发车，被联锁拒绝
        _result_event(1, 5.0, "depart", {"train_id": "T01", "station_id": "ST1"},
                      ok=False, violations=["间隙探测未确认，禁止发车"]),
        _result_event(2, 20.0, "gap_probe", {"train_id": "T01", "clear": True}, extra={"clear": True}),
        _result_event(3, 40.0, "depart", {"train_id": "T01", "station_id": "ST1"}),
    ]
    evaluation = evaluate(gap_alarm, problem, events, inject_time=0.0)
    assert any(v["type"] == "depart_with_unsafe_gap"
               for v in evaluation.critical_violations)
    assert evaluation.passed is False
    assert evaluation.rejected_actions  # 被拒动作也留痕


def test_extra_actions_recorded_but_not_penalized():
    """多做的非剧本动作不乱扣分。"""

    problem = dict(gap_alarm.target)
    events = [
        _result_event(1, 1.0, "sequence", {"previous": 1, "step": 1}),  # 多余动作
        _result_event(2, 10.0, "gap_probe", {"train_id": "T01", "clear": True}, extra={"clear": True}),
        _result_event(3, 20.0, "depart", {"train_id": "T01", "station_id": "ST1"}),
    ]
    evaluation = evaluate(gap_alarm, problem, events, inject_time=0.0)
    assert evaluation.passed is True
    assert any(e["action"] == "sequence" for e in evaluation.extras)


def test_recommendation_points_at_first_incomplete_step():
    problem = dict(psd_fault.target, psd_id="ST1-G01-01")
    events = [_result_event(1, 0.0, "fault_status", {"psd_id": "ST1-G01-01"})]
    evaluation = evaluate(psd_fault, problem, events, inject_time=0.0)
    advice = next_recommendation(psd_fault, evaluation)
    assert advice["step"] == "sweep"
    assert advice["action"] == "sweep"
    assert "ST1" in advice["rationale"] or "清扫" in advice["rationale"]
