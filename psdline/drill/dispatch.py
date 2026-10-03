"""处置动作分发表：把值班员动作翻译成演练平台上的真实调用。

关键：这里调用的是和现场**同一套** :class:`~psdline.app.Platform` 方法，演练
处置的可行性、联锁拒绝、配额、对位公差等行为与现场完全一致——演练不是简化
模拟器，而是在分叉副本上跑真代码。每个处理器返回一个结果字典，评估器据其
``ok`` / 字段判断处置是否达成意图。

动作表里额外补了两个只读/排查动作（``fault_status``、``inspect_gate``），它们
只查不改，用于规章里"先确认、再动手"的步骤。
"""

from __future__ import annotations

from typing import Callable

from ..errors import RailError
from .errors import EvaluateError

#: 处理器签名：(平台, 请求体) -> 结果字典
Handler = Callable[[object, dict], dict]


def _need(body: dict, key: str) -> str:
    value = body.get(key)
    if value is None or str(value) == "":
        raise EvaluateError(f"缺少必填参数 {key!r}", component="drill-dispatch")
    return str(value)


def _gate_locked(platform, psd_id: str) -> bool:
    gate = platform.gates.get(psd_id)
    return bool(gate and gate.locked)


# ---- 处理器 -----------------------------------------------------------

def _arrive(platform, body):
    return platform.arrive(_need(body, "train_id"), _need(body, "station_id"),
                           int(body.get("position_m", 0)))


def _depart(platform, body):
    return platform.depart(_need(body, "train_id"), _need(body, "station_id"))


def _gap_probe(platform, body):
    return platform.check_gap(_need(body, "train_id"), bool(body.get("clear", True)))


def _sweep(platform, body):
    station_id = _need(body, "station_id")
    result = platform.sweep(station_id)
    return {"ok": True, "station_id": station_id, **result}


def _clear_fault(platform, body):
    return platform.clear_fault(_need(body, "psd_id"))


def _fault_status(platform, body):
    psd_id = _need(body, "psd_id")
    gate = platform.gates.get(psd_id)
    if gate is None:
        return {"ok": False, "psd_id": psd_id, "violations": ["站台门不存在"]}
    fault = platform.faults
    is_fault = fault.state(psd_id) == "active" if hasattr(fault, "state") else _gate_locked(platform, psd_id)
    return {
        "ok": True, "psd_id": psd_id, "station": gate.station,
        "locked": gate.locked, "open": gate.open, "fault": bool(is_fault),
        "violations": [],
    }


def _inspect_gate(platform, body):
    """现场排查：只读查看门状态 + 当前联锁状态（不改动设备）。"""

    psd_id = _need(body, "psd_id")
    gate = platform.gates.get(psd_id)
    if gate is None:
        return {"ok": False, "psd_id": psd_id, "violations": ["站台门不存在"]}
    interlock = platform.status()["interlock"]
    return {
        "ok": True, "psd_id": psd_id, "station": gate.station,
        "locked": gate.locked, "open": gate.open,
        "inspection": "现场确认门体无卡滞、无异物，具备恢复条件",
        "interlock_locked_count": interlock.get("locked_count", 0),
        "violations": [],
    }


def _position(platform, body):
    return platform.refresh_position(_need(body, "train_id"),
                                     int(body.get("position_m", 0)))


def _arm(platform, body):
    return platform.arm(_need(body, "train_id"), bool(body.get("armed", True)))


def _enqueue(platform, body):
    return platform.enqueue(_need(body, "train_id"), _need(body, "id"),
                            str(body.get("kind", "open")), str(body.get("station_id", "")))


def _process(platform, body):
    return platform.process_queue(_need(body, "train_id"))


def _batch_open(platform, body):
    return platform.batch_open(_need(body, "station_id"))


def _renumber(platform, body):
    groups = body.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise EvaluateError("映射必须是非空对象", component="drill-dispatch")
    return platform.renumber(_need(body, "station_id"),
                             {str(k): str(v) for k, v in groups.items()})


def _sequence(platform, body):
    return platform.sequence_verdict(int(body.get("previous", 0)), int(body.get("step", 1)))


def _dwell(platform, body):
    return platform.register_dwell(_need(body, "train_id"), float(body.get("seconds", 0.0)),
                                   str(body.get("station_id", "")))


def _route(platform, body):
    return platform.assign_route(_need(body, "train_id"), _need(body, "origin"),
                                 _need(body, "destination"), int(body.get("distance_m", 0)))


#: 值班员可下发的全部演练动作 → 处理器。
DISPATCH: dict[str, Handler] = {
    "arrive": _arrive,
    "depart": _depart,
    "gap_probe": _gap_probe,
    "sweep": _sweep,
    "clear_fault": _clear_fault,
    "fault_status": _fault_status,
    "inspect_gate": _inspect_gate,
    "position": _position,
    "arm": _arm,
    "enqueue": _enqueue,
    "process": _process,
    "batch_open": _batch_open,
    "renumber": _renumber,
    "sequence": _sequence,
    "dwell": _dwell,
    "route": _route,
}

#: 处置动作集合（注入用的 set_fault 等教员动作不在这里，防止学员自导自演）。
TRAINEE_ACTIONS = frozenset(DISPATCH.keys())


def dispatch(platform, action: str, body: dict) -> dict:
    """在演练平台上执行一个动作；未知动作拒绝，RailError 收敛成失败结果。

    现场联锁条件不满足会抛 :class:`RailError`，这里转成 ``ok=False`` 的正常
    结果——"动作被现场拒绝"本身就是演练要评估的处置结果，不能让它冒泡成
    500，也不能被当成系统故障丢弃。
    """

    handler = DISPATCH.get(action)
    if handler is None:
        return {"ok": False, "action": action, "violations": [f"未知演练动作: {action}"]}
    try:
        result = handler(platform, body)
    except EvaluateError as error:
        # 参数缺失/非法：动作没有真的下发，记为一次失败处置（不升级成系统故障）。
        return {"ok": False, "action": action, "violations": [error.message],
                "component": error.component}
    except RailError as error:
        return {"ok": False, "action": action, "violations": [error.message],
                "component": error.component}
    if isinstance(result, dict):
        result.setdefault("ok", True)
        result.setdefault("violations", [])
        result["action"] = action
        return result
    return {"ok": True, "action": action, "value": result}
