"""``psdline drill`` 子命令：应急演练全流程命令行。

子命令一览：

``snapshot``   从现场只读捕获快照（``--kind fault_replay|live_fork``）；
``snapshots``  列出快照；
``start``      基于快照与场景开演；
``sessions``   列出演练会话；
``tick``       推进演练时钟并注入场景事件；
``act``        执行一步处置；
``note``       登记广播/联控等不改变现场的过程信息；
``status``     查看演练态势；
``abort``      一键中止演练并交还；
``finish``     正常结束并封存；
``report``     生成逐步复盘与评分；
``verify-audit`` 校验演练审计哈希链与封存签名；
``replay``     用快照 + 事件流独立重放，证明字节级可复现。

演练所有数据都在独立演练根（默认 ``var/drill``），与现场 ``var/state`` 分离。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from ..app import build_platform
from ..config import default_config, load_config
from ..errors import RailError
from ..store.jsonfile import JsonStore
from .canonical import digest
from .replay import replay_events
from .sandbox import fingerprint_store
from .scenario import SCENARIOS, get_scenario
from .service import DrillService
from .snapshot import SNAPSHOT_KIND_LIVE, SNAPSHOT_KIND_REPLAY


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _load(args) -> tuple:
    config = load_config(args.config) if getattr(args, "config", None) else default_config()
    live_root = getattr(args, "live_root", None) or config.storage_root
    live_store = JsonStore(live_root)
    drill_root = getattr(args, "drill_root", None) or "var/drill"
    return config, live_store, drill_root


def _service(args) -> DrillService:
    config, live_store, drill_root = _load(args)
    return DrillService(drill_root, live_store, config)


def _parse_json_arg(raw: str | None, name: str) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise SystemExit(f"--{name} 必须是合法 JSON: {error}")
    if not isinstance(value, dict):
        raise SystemExit(f"--{name} 必须是 JSON 对象")
    return value


def cmd_snapshot(args) -> int:
    service = _service(args)
    kind = SNAPSHOT_KIND_LIVE if args.kind == "live_fork" else SNAPSHOT_KIND_REPLAY
    snapshot_id = service.capture_snapshot(kind, args.label, note=args.note)
    _dump({"snapshot_id": snapshot_id, "kind": kind, "label": args.label})
    return 0


def cmd_snapshots(args) -> int:
    service = _service(args)
    rows = []
    for sid in service.snapshots.list_ids():
        manifest = service.snapshots.manifest(sid)
        rows.append(
            {
                "snapshot_id": sid,
                "kind": manifest.get("kind"),
                "label": manifest.get("label"),
                "docs": len(manifest.get("docs", [])),
                "captured_at": manifest.get("captured_at"),
                "fingerprint": manifest.get("fingerprint", "")[:16],
            }
        )
    _dump(rows)
    return 0


def cmd_start(args) -> int:
    service = _service(args)
    result = service.start_session(
        args.scenario,
        snapshot_id=args.snapshot,
        operator=args.operator,
        mode=args.mode,
        reason=args.reason,
    )
    _dump(result)
    return 0


def cmd_sessions(args) -> int:
    _dump(_service(args).list_sessions())
    return 0


def cmd_tick(args) -> int:
    _dump(_service(args).advance(args.session, args.to))
    return 0


def cmd_act(args) -> int:
    service = _service(args)
    meta = service._read_metadata(args.session)  # noqa: SLF001
    token = args.token or meta.get("token", {}).get("token", "")
    request_id = args.request_id or f"req-{uuid.uuid4().hex[:10]}"
    result = service.act(
        args.session,
        args.action,
        _parse_json_arg(args.args, "args"),
        client_request_id=request_id,
        token=token,
    )
    result["client_request_id"] = request_id
    _dump(result)
    return 0


def cmd_note(args) -> int:
    _dump(_service(args).note(args.session, args.text, args.channel))
    return 0


def cmd_status(args) -> int:
    _dump(_service(args).status(args.session))
    return 0


def cmd_abort(args) -> int:
    _dump(_service(args).finish(args.session, aborted=True, reason=args.reason))
    return 0


def cmd_finish(args) -> int:
    _dump(_service(args).finish(args.session, aborted=False, reason=args.reason))
    return 0


def cmd_report(args) -> int:
    _dump(_service(args).build_report(args.session))
    return 0


def cmd_verify_audit(args) -> int:
    service = _service(args)
    result = service.audit.verify()
    _dump(result)
    return 0 if result["ok"] else 3


def cmd_replay(args) -> int:
    config, live_store, drill_root = _load(args)
    service = DrillService(drill_root, live_store, config)
    meta = service._read_metadata(args.session)  # noqa: SLF001
    session_dir = service._session_dir(args.session)  # noqa: SLF001
    from .eventlog import ChainedEventLog

    scenario = get_scenario(meta["scenario"])
    events = ChainedEventLog(session_dir).events
    mutating = [e for e in events if e.kind in ("operator_action", "sim_tick")]
    out_root = args.out or f"{session_dir}/replay-output"
    result = replay_events(
        config,
        service.snapshots,
        meta["snapshot_id"],
        out_root,
        mutating,
        scenario,
        tick_seconds=scenario.tick_seconds,
    )
    _dump(
        {
            "session_id": args.session,
            "replayed_events": result["replayed"],
            "tick": result["tick"],
            "clock_at": result["clock_at"],
            "fingerprint": result["fingerprint"],
            "doc_count": result["doc_count"],
            "output_root": result["sandbox_root"],
        }
    )
    return 0


def cmd_field_fingerprint(args) -> int:
    _config, live_store, _drill_root = _load(args)
    _dump(fingerprint_store(live_store))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--live-root", help="现场存储根（默认取配置 storage_root）")
    common.add_argument("--drill-root", default="var/drill", help="演练独立存储根")
    parser = argparse.ArgumentParser(
        prog="psdline drill", description="应急演练（不触达现场）", parents=[common]
    )
    dsub = parser.add_subparsers(dest="drill_command", required=True)

    p = dsub.add_parser("snapshot", help="只读捕获现场快照")
    p.add_argument("--kind", choices=[SNAPSHOT_KIND_REPLAY, SNAPSHOT_KIND_LIVE], default=SNAPSHOT_KIND_REPLAY)
    p.add_argument("--label", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_snapshot)

    p = dsub.add_parser("snapshots", help="列出快照")
    p.set_defaults(func=cmd_snapshots)

    p = dsub.add_parser("start", help="开演")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="morning_peak_psd")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--operator", default="值班员")
    p.add_argument("--mode", choices=[SNAPSHOT_KIND_REPLAY, SNAPSHOT_KIND_LIVE], default=SNAPSHOT_KIND_REPLAY)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_start)

    p = dsub.add_parser("sessions", help="列出演练会话")
    p.set_defaults(func=cmd_sessions)

    p = dsub.add_parser("tick", help="推进演练时钟")
    p.add_argument("--session", required=True)
    p.add_argument("--to", type=int, required=True)
    p.set_defaults(func=cmd_tick)

    p = dsub.add_parser("act", help="执行一步处置")
    p.add_argument("--session", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--args", default="", help="JSON 对象形式的动作参数")
    p.add_argument("--request-id", default="")
    p.add_argument("--token", default="")
    p.set_defaults(func=cmd_act)

    p = dsub.add_parser("note", help="登记过程信息")
    p.add_argument("--session", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--channel", default="radio")
    p.set_defaults(func=cmd_note)

    p = dsub.add_parser("status", help="演练态势")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_status)

    p = dsub.add_parser("abort", help="一键中止演练")
    p.add_argument("--session", required=True)
    p.add_argument("--reason", default="值班员中止")
    p.set_defaults(func=cmd_abort)

    p = dsub.add_parser("finish", help="正常结束并封存")
    p.add_argument("--session", required=True)
    p.add_argument("--reason", default="演练完成")
    p.set_defaults(func=cmd_finish)

    p = dsub.add_parser("report", help="逐步复盘与评分")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_report)

    p = dsub.add_parser("verify-audit", help="校验演练审计链与封存签名")
    p.set_defaults(func=cmd_verify_audit)

    p = dsub.add_parser("replay", help="独立重放证明可复现")
    p.add_argument("--session", required=True)
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_replay)

    p = dsub.add_parser("field-fingerprint", help="打印现场存储只读指纹")
    p.set_defaults(func=cmd_field_fingerprint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RailError as error:
        print(json.dumps(error.as_record(), ensure_ascii=False), file=sys.stderr)
        return 2
