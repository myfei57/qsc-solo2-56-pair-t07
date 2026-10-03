"""演练复盘：逐步记录与评分。

把哈希链事件翻译成值班员视角的“一步一结果 + 判断依据”，并按场景评分清单打分。
评分全部来自**不可变事件流**，不读取工作沙箱（报告不依赖可被重建的当前态，只
依赖被封存的历史）。
"""

from __future__ import annotations


def step_records(events) -> list[dict]:
    """把事件流转成复盘用的逐步记录。"""

    records: list[dict] = []
    for event in events:
        if event.kind == "session_begin":
            records.append(
                {
                    "seq": event.seq,
                    "kind": "begin",
                    "tick": event.sim_tick,
                    "title": "演练开始",
                    "detail": {
                        "scenario": event.payload.get("scenario"),
                        "snapshot_id": event.payload.get("snapshot_id"),
                        "operator": event.payload.get("operator"),
                        "mode": event.payload.get("mode"),
                    },
                }
            )
        elif event.kind == "operator_action":
            payload = event.payload
            result = payload.get("result") or {}
            gate = payload.get("gate") or {}
            basis = payload.get("basis") or {}
            records.append(
                {
                    "seq": event.seq,
                    "kind": "action",
                    "tick": event.sim_tick,
                    "action": payload.get("action"),
                    "client_request_id": payload.get("client_request_id"),
                    "executed": payload.get("executed", False),
                    "ok": result.get("ok"),
                    "violations": result.get("violations", []),
                    "transmitted": gate.get("transmitted", False),
                    "gate_reason": gate.get("reason"),
                    "rule": basis.get("rule"),
                    "preconditions": basis.get("preconditions", []),
                    "evidence": basis.get("evidence", {}),
                    "anticipated": basis.get("anticipated"),
                    "basis_matches_result": _basis_matches(basis, result, payload.get("executed", False)),
                    "args": payload.get("args", {}),
                }
            )
        elif event.kind == "sim_tick":
            injected = []
            for effect in event.payload.get("effects", []):
                for item in effect.get("injected", []):
                    injected.append({"tick": effect["tick"], **item})
            records.append(
                {
                    "seq": event.seq,
                    "kind": "tick",
                    "tick": event.sim_tick,
                    "title": f"推进到第 {event.sim_tick} 步",
                    "injected": injected,
                }
            )
        elif event.kind == "note":
            records.append(
                {
                    "seq": event.seq,
                    "kind": "note",
                    "tick": event.sim_tick,
                    "channel": event.payload.get("channel"),
                    "text": event.payload.get("text"),
                }
            )
        elif event.kind in ("session_end", "session_abort"):
            records.append(
                {
                    "seq": event.seq,
                    "kind": "end" if event.kind == "session_end" else "abort",
                    "tick": event.sim_tick,
                    "reason": event.payload.get("reason", ""),
                }
            )
    return records


def _basis_matches(basis: dict, result: dict, executed: bool) -> object:
    """预判（按规则）与真实执行结果是否一致；不一致标 None（无前置判据的动作）。"""

    if not basis or not executed:
        return None
    anticipated = basis.get("anticipated")
    if anticipated is None:
        return None
    return bool(anticipated) == bool(result.get("ok"))


def score_scenario(scenario, events) -> dict:
    """按场景评分清单对处置过程打分。"""

    action_events = [e for e in events if e.kind == "operator_action"]
    # 记录每个动作首次成功 / 任意发生的 tick。
    first_success: dict[str, int] = {}
    first_attempt: dict[str, int] = {}
    for event in action_events:
        action = event.payload.get("action")
        result = event.payload.get("result") or {}
        first_attempt.setdefault(action, event.sim_tick)
        if result.get("ok") and action not in first_success:
            first_success[action] = event.sim_tick

    # 故障注入 tick：来自 sim_tick 事件。
    fault_tick = _inject_tick(events, "inject_fault")
    clear_tick = first_success.get("clear_fault")

    total_violations = 0
    blocked_attempts = 0
    for event in action_events:
        result = event.payload.get("result") or {}
        violation_count = len(result.get("violations", []))
        if result.get("ok"):
            total_violations += violation_count
        elif violation_count:
            blocked_attempts += 1

    items: list[dict] = []
    earned = 0
    for item in scenario.checklist:
        passed, why = _evaluate(
            item, events, action_events, first_success, first_attempt, fault_tick, clear_tick
        )
        if passed:
            earned += item.weight
        items.append(
            {"code": item.code, "text": item.text, "expect": item.expect, "weight": item.weight,
             "passed": passed, "why": why}
        )

    max_score = sum(item.weight for item in scenario.checklist)
    return {
        "items": items,
        "score": earned,
        "max_score": max_score,
        "passed": earned == max_score and total_violations == 0,
        "violations_total": total_violations,
        "blocked_attempts": blocked_attempts,
    }


def _inject_tick(events, kind: str) -> int | None:
    for event in events:
        if event.kind != "sim_tick":
            continue
        for effect in event.payload.get("effects", []):
            for injected in effect.get("injected", []):
                if injected.get("event_kind") == kind:
                    return effect["tick"]
    return None


def _evaluate(item, events, action_events, first_success, first_attempt, fault_tick, clear_tick):
    ref = item.reference
    if item.expect == "must_do":
        tick = first_success.get(item.action)
        if tick is None:
            return False, f"未成功执行 {item.action}"
        return True, f"第 {tick} 步成功执行 {item.action}"

    if item.expect == "must_ack":
        if ref == "inject_fault:2":
            # 故障后必须有针对相关门/站台的处置动作（sweep/clear_fault/check_gap 等）。
            reacted = [
                e for e in action_events
                if e.sim_tick >= (fault_tick or 0)
                and e.payload.get("action") in {"sweep", "clear_fault", "check_gap", "set_fault", "probe_gap"}
            ]
            return (bool(reacted), "已对故障做出处置确认" if reacted else "未见对故障的处置确认")
        if ref == "no-violations":
            # 只统计“成功发生”的违规/越序；被门控或现场联锁正确拦停的尝试不算失误，
            # 那正说明联锁有效、处置被安全约束住了。
            bad = [
                e for e in action_events
                if (e.payload.get("result") or {}).get("ok")
                and (e.payload.get("result") or {}).get("violations")
            ]
            blocked = [
                e for e in action_events
                if not (e.payload.get("result") or {}).get("ok")
                and (e.payload.get("result") or {}).get("violations")
            ]
            if bad:
                return False, f"成功越序/违规 {len(bad)} 次"
            return True, (
                "全程无成功违规"
                + (f"，{len(blocked)} 次危险尝试被联锁正确拦停" if blocked else "")
            )
        # 通用：有任意动作即视为参与处置。
        return (len(action_events) > 0, "有处置记录" if action_events else "无处置记录")

    if item.expect == "must_not_do_before":
        if item.action == "depart" and fault_tick is not None:
            bound = clear_tick if clear_tick is not None else 10 ** 9
            bad = [
                e for e in action_events
                if e.payload.get("action") == "depart"
                and fault_tick <= e.sim_tick < bound
                and (e.payload.get("result") or {}).get("ok")
            ]
            # 现场联锁本身也会拦住违规发车；无论“尝试被拦”还是“成功放跑”都算问题，
            # 但被门控/联锁成功拦停的，风险降级为提示。
            succeeded = [e for e in bad if (e.payload.get("result") or {}).get("ok")]
            if succeeded:
                return False, f"故障未解除前成功发车 {len(succeeded)} 次（严重）"
            attempted = [
                e for e in action_events
                if e.payload.get("action") == "depart" and fault_tick <= e.sim_tick < bound
            ]
            if attempted:
                return True, "尝试发车但被联锁正确拦停"
            return True, "故障解除前未发车"
        return True, "无相关禁止动作"

    return False, "未知评分项"
