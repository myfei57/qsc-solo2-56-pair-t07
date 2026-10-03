"""演练命令行入口。

``psdline drill`` 子命令把演练服务的能力暴露给值班终端：

    scenarios              列出剧本
    sessions               列出演练会话
    snapshots              列出故障时刻快照
    start --scenario K --mode shadow|replay [--snapshot H]  创建/准备/开始
    act SID --action A --json '{...}'  下发处置动作
    tick SID [--dt N]      推进世界（replay 手动步进）
    hint SID               请求下一步标准动作提示
    suspend / resume SID   挂起 / 恢复
    seal SID               封存并出成绩单
    break-glass SID --confirm-by X --reason ...  双人破玻退出
    verify SID             深度复核（哈希链+重放+世界指纹）
    timeline / report SID  查看事件链 / 只读复盘报告
    world SID              演练世界当前态（shadow 附真实现场只读对照）
    recover                服务重启后恢复（通常启动时自动跑，也可手动触发）

所有命令只读写演练目录，现场存储只读地用于 shadow 拍快照。
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..app import Platform
from ..drill import DrillService, MODE_REPLAY, MODE_SHADOW
from ..drill.errors import DrillError


def dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_service(platform: Platform, config, *, shadow_speed: float = 1.0) -> DrillService:
    """按存储根推导演练根（现场根的同级 ``drill`` 目录），构造服务并先做恢复。"""

    live_root = os.path.abspath(platform.store.root)
    drill_root = os.path.join(os.path.dirname(live_root.rstrip(os.sep)), "drill")
    service = DrillService(drill_root, platform, config, shadow_speed=shadow_speed)
    service.recover_all()
    return service


def register(subparsers) -> None:
    """把 ``drill`` 子命令注册进命令行解析器。"""

    drill = subparsers.add_parser("drill", help="应急演练（与现场隔离，动作不外发）")
    dsub = drill.add_subparsers(dest="drill_command", required=True)

    dsub.add_parser("scenarios", help="列出演练剧本")
    dsub.add_parser("sessions", help="列出演练会话")
    dsub.add_parser("snapshots", help="列出故障时刻快照")
    dsub.add_parser("recover", help="扫描并恢复上次崩溃留下的会话")

    start = dsub.add_parser("start", help="创建、准备并开始一场演练")
    start.add_argument("--scenario", required=True)
    start.add_argument("--mode", choices=[MODE_SHADOW, MODE_REPLAY], default=MODE_REPLAY)
    start.add_argument("--snapshot", default="", help="replay 模式指定快照指纹")
    start.add_argument("--actor", default="dispatcher")
    start.add_argument("--title", default="")

    act = dsub.add_parser("act", help="下发一个演练处置动作（不进现场）")
    act.add_argument("session_id")
    act.add_argument("--action", required=True)
    act.add_argument("--json", default="{}", help="动作参数 JSON")
    act.add_argument("--actor", default="dispatcher")

    tick = dsub.add_parser("tick", help="推进演练世界（replay 手动步进）")
    tick.add_argument("session_id")
    tick.add_argument("--dt", type=float, default=1.0)

    hint = dsub.add_parser("hint", help="请求下一步标准动作提示")
    hint.add_argument("session_id")
    hint.add_argument("--actor", default="dispatcher")

    for name in ("suspend", "resume", "seal"):
        parser = dsub.add_parser(name, help=f"{name} 一场演练")
        parser.add_argument("session_id")
        parser.add_argument("--actor", default="dispatcher")
        if name == "suspend":
            parser.add_argument("--reason", default="")

    bg = dsub.add_parser("break-glass", help="双人破玻退出（不可逆）")
    bg.add_argument("session_id")
    bg.add_argument("--actor", required=True)
    bg.add_argument("--confirm-by", required=True, help="第二名确认人（必须不同于 actor）")
    bg.add_argument("--reason", default="值班长令退出演练")

    for name in ("timeline", "report", "world", "verify"):
        parser = dsub.add_parser(name, help=f"{name}")
        parser.add_argument("session_id")

    return drill


def run(command: str, args, service: DrillService) -> int:
    """执行一个 drill 子命令。返回进程退出码。``command`` 仅用于日志区分。"""

    c = args.drill_command
    if c == "scenarios":
        dump({"scenarios": service.catalog()})
        return 0
    if c == "sessions":
        dump({"sessions": service.list_sessions(), "drill_root": service.root})
        return 0
    if c == "snapshots":
        dump({"snapshots": service.snapshots.list()})
        return 0
    if c == "recover":
        dump({"recovered": service.recover_all()})
        return 0

    if c == "start":
        meta = service.create_session(
            args.scenario, args.actor, args.mode,
            title=args.title, snapshot_digest=args.snapshot)
        prepared = service.prepare(meta["id"])
        started = service.start(meta["id"], args.actor)
        dump({
            "session": meta["id"], "state": started["meta"]["state"],
            "mode": meta["mode"], "snapshot": meta["snapshot_digest"],
            "problem": started["meta"].get("problem", prepared.get("problem")),
            "injection": started["injection"],
            "briefing": prepared.get("briefing", ""),
            "hint": "使用 act <session> --action ... 下发动作；replay 用 tick 推进",
        })
        return 0

    sid = args.session_id
    if c == "act":
        try:
            params = json.loads(getattr(args, "json", "{}") or "{}")
        except json.JSONDecodeError as error:
            raise DrillError(f"--json 不是合法 JSON: {error}", component="drill-cli")
        if not isinstance(params, dict):
            raise DrillError("--json 必须是对象", component="drill-cli")
        dump(service.act(sid, args.action, params, args.actor))
        return 0
    if c == "tick":
        dump(service.tick(sid, args.dt))
        return 0
    if c == "hint":
        dump(service.hint(sid, args.actor))
        return 0
    if c == "suspend":
        dump(service.suspend(sid, args.actor, args.reason))
        return 0
    if c == "resume":
        dump(service.resume(sid, args.actor))
        return 0
    if c == "seal":
        sealed = service.seal(sid, args.actor)
        dump({"meta": sealed["meta"], "report": sealed["report"]})
        return 0
    if c == "break-glass":
        dump(service.break_glass(sid, args.actor, args.confirm_by, args.reason))
        return 0
    if c == "timeline":
        dump({"timeline": service.timeline(sid)})
        return 0
    if c == "report":
        dump(service.report(sid))
        return 0
    if c == "world":
        dump(service.world_view(sid))
        return 0
    if c == "verify":
        dump(service.verify_session(sid))
        return 0
    raise DrillError(f"未知 drill 子命令: {c}", component="drill-cli")
