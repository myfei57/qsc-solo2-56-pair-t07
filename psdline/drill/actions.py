"""值班员动作在沙箱平台上的真实执行器。

演练只允许下面这一个函数触达沙箱业务逻辑，便于把“现场真实调用链”和“演练调度”
清楚分开。每个动作映射到 :class:`~psdline.app.Platform` 上**同名或显式映射**的
方法——值班员练的就是真系统上的那套操作。返回值统一裁剪成可落盘、可复盘的字典。
"""

from __future__ import annotations

from ..app import Platform
from ..errors import RailError


class ActionError(RailError):
    """演练动作未登记或参数缺失。"""


# 需要的命名参数（缺一个就在演练边界报出，而不是在平台深处抛 TypeError）。
ACTION_SPECS: dict[str, tuple[str, ...]] = {
    "arrive": ("train_id", "station_id", "position_m"),
    "depart": ("train_id", "station_id"),
    "doors_open": ("train_id", "station_id"),
    "check_gap": ("train_id",),
    "probe_gap": ("train_id",),
    "enqueue": ("train_id", "command_id", "kind"),
    "process_queue": ("train_id",),
    "set_fault": ("psd_id",),
    "clear_fault": ("psd_id",),
    "batch_open": ("station_id",),
    "sweep": ("station_id",),
    "arm": ("train_id", "armed"),
    "refresh_position": ("train_id", "position_m"),
    "renumber": ("station_id", "groups"),
    "sequence_verdict": ("previous", "step"),
    "register_dwell": ("train_id", "seconds"),
    "assign_route": ("train_id", "origin", "destination", "distance_m"),
}


def execute(platform: Platform, action: str, args: dict) -> dict:
    """在沙箱平台执行一步动作，返回统一结果（含 ok/violations）。"""

    if action not in ACTION_SPECS:
        raise ActionError(f"未登记的演练动作: {action}", component="drill.engine")
    required = ACTION_SPECS[action]
    missing = [name for name in required if name not in args]
    if missing:
        raise ActionError(f"动作 {action} 缺少参数: {missing}", component="drill.engine")

    if action == "arrive":
        report = platform.arrive(args["train_id"], args["station_id"], args["position_m"])
    elif action == "depart":
        report = platform.depart(args["train_id"], args["station_id"])
    elif action == "doors_open":
        report = platform.doors_open(args["train_id"], args["station_id"])
    elif action == "check_gap":
        report = platform.check_gap(args["train_id"], True)
    elif action == "probe_gap":
        report = platform.check_gap(args["train_id"], False)
    elif action == "enqueue":
        report = platform.enqueue(
            args["train_id"], args["command_id"], args["kind"], args.get("station_id", "")
        )
    elif action == "process_queue":
        report = platform.process_queue(args["train_id"])
    elif action == "set_fault":
        report = platform.set_fault(args["psd_id"])
    elif action == "clear_fault":
        report = platform.clear_fault(args["psd_id"])
    elif action == "batch_open":
        report = platform.batch_open(args["station_id"])
    elif action == "sweep":
        report = platform.sweep(args["station_id"])
    elif action == "arm":
        report = platform.arm(args["train_id"], bool(args["armed"]))
    elif action == "refresh_position":
        report = platform.refresh_position(args["train_id"], args["position_m"])
    elif action == "renumber":
        report = platform.renumber(args["station_id"], dict(args["groups"]))
    elif action == "sequence_verdict":
        report = platform.sequence_verdict(int(args["previous"]), int(args["step"]))
    elif action == "register_dwell":
        report = platform.register_dwell(
            args["train_id"], float(args["seconds"]), args.get("station_id", "")
        )
    else:  # assign_route
        report = platform.assign_route(
            args["train_id"], args["origin"], args["destination"], int(args["distance_m"])
        )

    return _normalize(report)


def _normalize(report: dict) -> dict:
    """把平台返回值收敛成 JSON 安全的统一形状。"""

    def clean(value):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return str(value)

    return {
        "ok": bool(report.get("ok", True)),
        "violations": [str(v) for v in report.get("violations", [])],
        "report": clean(report),
    }
