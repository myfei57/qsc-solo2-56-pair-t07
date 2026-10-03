#!/usr/bin/env python3
"""应急演练自检脚本（不依赖 pytest，交付前直接 ``make drill-demo``）。

用临时目录准备一个“现场”，再完整演练早高峰站台门故障处置，最后断言：
现场存储一个字节没变、现场出口零触达、演练满分、重放字节一致、审计链可验。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psdline.app import build_platform  # noqa: E402
from psdline.config import default_config  # noqa: E402
from psdline.drill.sandbox import fingerprint_store  # noqa: E402
from psdline.drill.service import DrillService  # noqa: E402
from psdline.drill.snapshot import SNAPSHOT_KIND_REPLAY  # noqa: E402


def main() -> int:
    root = tempfile.mkdtemp(prefix="psdline-drill-demo-")
    live_root = os.path.join(root, "live")
    drill_root = os.path.join(root, "drill")
    config = default_config()

    # 现场：一趟已到站开门、间隙已确认的列车。
    live = build_platform(config, storage_root=live_root)
    live.start()
    live.arrive("T01", "ST1", 0)
    live.check_gap("T01")
    before = fingerprint_store(live.store)

    service = DrillService(drill_root, live.store, config)
    snap = service.capture_snapshot(SNAPSHOT_KIND_REPLAY, "早高峰站台门故障时刻")
    started = service.start_session("morning_peak_psd", snapshot_id=snap, operator="值班员")
    sid, token = started["session_id"], started["token"]["token"]

    def act(action, args, rid):
        return service.act(sid, action, args, rid, token)

    # 演练处置序列（全部只在沙箱里）。
    act("arrive", {"train_id": "T09", "station_id": "ST1", "position_m": 0}, "r1")
    act("doors_open", {"train_id": "T09", "station_id": "ST1"}, "r2")
    service.advance(sid, 3)  # 注入故障：ST1-G01-01 锁定 + 客流告警
    act("sweep", {"station_id": "ST1"}, "r3")
    blocked = act("depart", {"train_id": "T09", "station_id": "ST1"}, "r4")
    service.advance(sid, 18)  # 设备物理恢复
    act("check_gap", {"train_id": "T09"}, "r5")
    act("clear_fault", {"psd_id": "ST1-G01-01"}, "r6")
    act("depart", {"train_id": "T09", "station_id": "ST1"}, "r7")

    ended = service.finish(sid)
    report = ended["report"]
    after = fingerprint_store(live.store)
    audit_ok = service.audit.verify()["ok"]

    checks = {
        "现场存储零改动": after["fingerprint"] == before["fingerprint"],
        "现场出口零触达": report["field_transmitted"] == 0,
        "违规发车被联锁拦停": blocked["ok"] is False,
        "演练满分通过": report["passed"] and report["score"] == report["max_score"],
        "重放字节一致": report["integrity"]["replay_match"],
        "演练审计链可验": audit_ok,
    }
    print(json.dumps({"drill_root": drill_root, "checks": checks,
                      "score": f"{report['score']}/{report['max_score']}"},
                     ensure_ascii=False, indent=2))
    shutil.rmtree(root, ignore_errors=True)
    if not all(checks.values()):
        print("演练自检未通过", file=sys.stderr)
        return 1
    print("演练自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
