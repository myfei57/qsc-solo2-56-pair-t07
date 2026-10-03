"""每步处置的「判断依据」：把真实联锁规则翻译成可追溯的前置判据。

演练复盘要回答的不只是“这步成没成”，还有“系统凭什么这么判”。本模块在动作执行
**之前**，从沙箱当前态取证据，按现场真实规则做纯查询式预判，输出：

* ``rule``        规则码（对应联锁/门控/间隙的硬性约束）；
* ``evidence``    取自沙箱现场的事实（门状态、间隙状态、配额、对位…）；
* ``preconditions`` 每条前置条件的满足情况；
* ``anticipated`` 预判会不会被现场逻辑拒绝。

这些判据**不改变任何状态**，与动作的真实执行结果分别落盘；二者一致说明推演忠实，
不一致则是引擎缺陷，会在复盘里被标红。规则码与 :mod:`psdline.interlock`、
:mod:`psdline.door.policy`、:mod:`psdline.gap` 的实现一一对应。
"""

from __future__ import annotations

from typing import Any

from ..door.policy import depart_policy
from ..interlock.clear import releasable

# 规则码（与现场实现对应，复盘时直接引用）。
RULE_QUOTA = "QUOTA-ARRIVE-DEPART"
RULE_NAMESPACE = "NS-STATION-REQUIRED"
RULE_ALIGN = "ALIGN-WITHIN-TOLERANCE"
RULE_DOOR_POLICY = "DOOR-OPEN-CLOSE-QUEUE"
RULE_GAP = "GAP-CLEAR-BEFORE-DEPART"
RULE_RELEASE = "INTERLOCK-RELEASE-GATE-CLOSED-GAP-CLEAR"
RULE_FAULT_EXISTS = "INTERLOCK-CLEAR-ONLY-IF-FAULT"
RULE_SEQUENCE = "SEQ-MONOTONE-MOD65536"


def _gate_state(platform, psd_id: str) -> dict[str, Any]:
    gate = platform.gates.get(psd_id)
    if gate is None:
        return {"exists": False}
    return {"exists": True, "id": gate.id, "open": bool(gate.open), "locked": bool(gate.locked)}


def _quota(platform, name: str) -> dict:
    status = platform.quotas.status(name)
    return {
        "configured": bool(status.get("configured")),
        "limit": status.get("limit"),
        "remaining": status.get("remaining", 0) if status.get("configured") else 0,
    }


def build_basis(platform, action: str, args: dict) -> dict:
    """根据动作类型收集执行前的规则判据。"""

    builder = _BUILDERS.get(action)
    if builder is None:
        return {
            "rule": "GENERAL-NO-EXTRA-PRECONDITION",
            "evidence": {},
            "preconditions": [],
            "anticipated": True,
            "note": "该动作不携带额外联锁前置条件，仍受门控与命名空间约束。",
        }
    return builder(platform, args)


def _cond(name: str, passed: bool, detail: Any = None) -> dict:
    return {"condition": name, "passed": bool(passed), "detail": detail}


def _basis_arrive(platform, args: dict) -> dict:
    station_id = str(args.get("station_id", ""))
    conds = [
        _cond("到站配额>0", _quota(platform, "arrive")["remaining"] > 0, _quota(platform, "arrive")),
        _cond(
            "站台在命名空间内",
            station_id in platform.namespace.station_ids(),
            {"station_id": station_id, "known": platform.namespace.station_ids()},
        ),
        _cond(
            "站台已登记布局",
            platform.stations.get(station_id) is not None,
            {"station_id": station_id},
        ),
    ]
    return {
        "rule": f"{RULE_QUOTA}; {RULE_NAMESPACE}; {RULE_ALIGN}",
        "evidence": {"quota_arrive": _quota(platform, "arrive")},
        "preconditions": conds,
        "anticipated": all(c["passed"] for c in conds),
    }


def _basis_depart(platform, args: dict) -> dict:
    train_id = str(args.get("train_id", ""))
    policy = depart_policy(platform.doors, train_id)
    gap_state = platform.gaps.state(train_id)
    verdict_prev = {"aspect": None}
    conds = [
        _cond("发车配额>0", _quota(platform, "depart")["remaining"] > 0, _quota(platform, "depart")),
        _cond(
            "门控自检(开过门/关门落盘/队列空)",
            policy["allowed"],
            policy["checks"],
        ),
        _cond("间隙已确认无异常", gap_state == "clear", {"gap": gap_state}),
    ]
    return {
        "rule": f"{RULE_DOOR_POLICY}; {RULE_GAP}; {RULE_QUOTA}",
        "evidence": {"door_policy": policy, "gap": gap_state},
        "preconditions": conds,
        "anticipated": all(c["passed"] for c in conds),
    }


def _basis_check_gap(platform, args: dict) -> dict:
    train_id = str(args.get("train_id", ""))
    aligned = platform.store.exists(f"train/{train_id}/align")
    return {
        "rule": "GAP-REQUIRES-ALIGNMENT",
        "evidence": {"aligned": aligned},
        "preconditions": [_cond("列车已对位落盘", aligned, {"train_id": train_id})],
        "anticipated": aligned,
    }


def _basis_clear_fault(platform, args: dict) -> dict:
    psd_id = str(args.get("psd_id", ""))
    gate = platform.gates.get(psd_id)
    fault_state = platform.faults.state(psd_id)
    gap_clear = not platform.gaps.blocked_ids()
    allowed, reason = (releasable(platform.gates, psd_id, gap_clear) if gate else (False, "gate_missing"))
    conds = [
        _cond("门区存在", gate is not None),
        _cond("存在置位故障记录", fault_state == "active", {"fault": fault_state}),
        _cond("门已回关闭位", gate is not None and not gate.open),
        _cond("无间隙探测异常", gap_clear, {"blocked": platform.gaps.blocked_ids()}),
    ]
    return {
        "rule": f"{RULE_RELEASE}; {RULE_FAULT_EXISTS}",
        "evidence": {"gate": _gate_state(platform, psd_id), "fault": fault_state, "release_reason": reason},
        "preconditions": conds,
        "anticipated": allowed and fault_state == "active",
    }


def _basis_set_fault(platform, args: dict) -> dict:
    psd_id = str(args.get("psd_id", ""))
    gate = platform.gates.get(psd_id)
    return {
        "rule": "INTERLOCK-SET-FAULT-LOCKS-GATE",
        "evidence": {"gate": _gate_state(platform, psd_id)},
        "preconditions": [_cond("门区存在", gate is not None)],
        "anticipated": gate is not None,
    }


def _basis_doors_open(platform, args: dict) -> dict:
    train_id = str(args.get("train_id", ""))
    aligned = platform.store.exists(f"train/{train_id}/align")
    return {
        "rule": "DOOR-OPEN-REQUIRES-ALIGNMENT",
        "evidence": {"aligned": aligned},
        "preconditions": [_cond("列车已对位落盘", aligned, {"train_id": train_id})],
        "anticipated": aligned,
    }


def _basis_sequence(platform, args: dict) -> dict:
    previous = int(args.get("previous", 0))
    step = int(args.get("step", 0))
    current = (previous + step) % 65536
    advanced = current != previous and 0 <= step < 65536
    return {
        "rule": RULE_SEQUENCE,
        "evidence": {"previous": previous, "step": step, "current": current},
        "preconditions": [_cond("序号单调推进且不回绕错位", advanced)],
        "anticipated": advanced,
    }


_BUILDERS = {
    "arrive": _basis_arrive,
    "depart": _basis_depart,
    "check_gap": _basis_check_gap,
    "probe_gap": _basis_check_gap,
    "clear_fault": _basis_clear_fault,
    "set_fault": _basis_set_fault,
    "doors_open": _basis_doors_open,
    "sequence_verdict": _basis_sequence,
}
