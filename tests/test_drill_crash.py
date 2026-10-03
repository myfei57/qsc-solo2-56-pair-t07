"""跨进程真实崩溃恢复测试。

不同于进程内模拟，这里真正 spawn 一个子进程，让它在"处置意图已 fsync、结果
尚未写入"的瞬间 ``os._exit(9)``（kill -9 语义：不跑 finally/atexit）。然后父
进程用一个全新服务实例恢复，验证 fail-closed 恢复契约。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.drill import DrillService, MODE_REPLAY

WORKER = str(Path(__file__).resolve().parents[1] / "tools" / "crash_worker.py")


@pytest.fixture()
def crash_env(tmp_path):
    live = build_platform(default_config(), clock=ManualClock(),
                          storage_root=str(tmp_path / "live"))
    drill_root = str(tmp_path / "drill")
    service = DrillService(drill_root, live, shadow_speed=0.0)
    digest = service.snapshots.capture_from_live(live.store, source="crash-seed").digest
    meta = service.create_session("psd_fault", "dispatcher", MODE_REPLAY,
                                  snapshot_digest=digest)
    service.prepare(meta["id"])
    started = service.start(meta["id"], "dispatcher")
    yield service, live, drill_root, meta["id"], started["injection"]["psd_id"]
    service.shutdown()


def _run_worker(drill_root, session_id, phase):
    return subprocess.run(
        [sys.executable, WORKER, drill_root, session_id, phase],
        capture_output=True, text=True, timeout=30,
    )


def test_cross_process_hard_kill_then_safe_recovery(crash_env):
    service, live, drill_root, sid, psd = crash_env

    acted = _run_worker(drill_root, sid, "act")
    assert acted.returncode == 0
    assert "ACTED" in acted.stdout

    crashed = _run_worker(drill_root, sid, "crash")
    assert crashed.returncode == 9  # os._exit(9)

    # 全新进程：不共享任何内存
    recovered = DrillService(drill_root, live, shadow_speed=0.0)
    results = recovered.recover_all()
    mine = [item for item in results if item["session"] == sid]
    assert mine and mine[0]["outcome"] == "suspended"

    meta = recovered.registry.get(sid)
    assert meta["state"] == "suspended"
    assert meta["recovered"] is True
    assert "req-killed" in meta["recovery_note"]
    assert "未自动重发" in meta["recovery_note"]

    # 故障门必须仍锁定（崩溃的清故障没有被自动重放生效）
    runtime = recovered._open_runtime(sid, meta)
    assert runtime.platform.gates.get(psd).locked is True

    # 显式恢复后继续，最终可封存且深度复核通过
    recovered.resume(sid, "dispatcher")
    recovered.act(sid, "inspect_gate", {"psd_id": psd}, "dispatcher")
    recovered.act(sid, "clear_fault", {"psd_id": psd}, "dispatcher")
    recovered.act(sid, "depart", {"train_id": "T01", "station_id": "ST1"}, "dispatcher")
    sealed = recovered.seal(sid, "dispatcher")
    assert sealed["report"]["evaluation"]["passed"] is True
    verification = recovered.verify_session(sid)
    assert verification["passed"] is True
    assert verification["chain_intact"] is True


def test_worker_normal_actions_are_durable_across_processes(crash_env):
    service, live, drill_root, sid, psd = crash_env
    result = _run_worker(drill_root, sid, "act")
    assert result.returncode == 0
    # 新进程能看到子进程落盘的动作（事件链里有 sweep / fault_status）
    fresh = DrillService(drill_root, live, shadow_speed=0.0)
    timeline = fresh.timeline(sid)
    actions = [e["payload"].get("action") for e in timeline]
    assert "sweep" in actions
    assert "fault_status" in actions
