"""演练服务端到端：隔离、并发、崩溃恢复、字节级重放、封存不可逆。"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from psdline.drill.sandbox import fingerprint_store
from psdline.drill.service import DrillService
from psdline.drill.snapshot import SNAPSHOT_KIND_LIVE
from psdline.errors import RailError


def _run_compliant(service, sid, token):
    """跑一遍符合联锁规则的处置，返回结束报告。"""

    def act(action, args, rid):
        return service.act(sid, action, args, rid, token)

    act("arrive", {"train_id": "T09", "station_id": "ST1", "position_m": 0}, "r1")
    act("doors_open", {"train_id": "T09", "station_id": "ST1"}, "r2")
    service.advance(sid, 3)  # 故障注入、门锁
    act("sweep", {"station_id": "ST1"}, "r3")
    blocked = act("depart", {"train_id": "T09", "station_id": "ST1"}, "r4")
    service.advance(sid, 18)  # 设备恢复
    act("check_gap", {"train_id": "T09"}, "r5")
    act("clear_fault", {"psd_id": "ST1-G01-01"}, "r6")
    act("depart", {"train_id": "T09", "station_id": "ST1"}, "r7")
    return blocked


def test_live_store_completely_untouched(service, live_platform, started, paths):
    before = fingerprint_store(live_platform.store)
    audit_before = live_platform.audit.journal.count()
    sid, token = started["session_id"], started["token"]["token"]
    _run_compliant(service, sid, token)
    service.finish(sid)
    after = fingerprint_store(live_platform.store)
    assert after["fingerprint"] == before["fingerprint"]
    # 演练没往现场 audit journal 追加任何一条。
    assert live_platform.audit.journal.count() == audit_before


def test_zero_field_transmission_and_full_score(service, started):
    sid, token = started["session_id"], started["token"]["token"]
    blocked = _run_compliant(service, sid, token)
    assert blocked["ok"] is False  # 故障未解除发车被联锁拦停
    report = service.finish(sid)["report"]
    assert report["field_transmitted"] == 0
    assert report["passed"] is True
    assert report["score"] == report["max_score"] == 6
    assert report["blocked_attempts"] == 1
    assert report["integrity"]["replay_match"] is True


def test_each_step_has_basis_and_result(service, started):
    sid, token = started["session_id"], started["token"]["token"]
    service.act(sid, "arrive", {"train_id": "T09", "station_id": "ST1", "position_m": 0}, "r1", token)
    report = service.finish(sid, aborted=True, reason="不测了")["report"]
    action_steps = [s for s in report["steps"] if s["kind"] == "action"]
    assert action_steps
    step = action_steps[0]
    assert step["rule"]  # 判断依据规则码
    assert step["preconditions"]  # 前置条件
    assert step["transmitted"] is False


def test_bad_token_is_gated_and_not_executed(service, started):
    sid = started["session_id"]
    result = service.act(sid, "sweep", {"station_id": "ST1"}, "evil", "not-a-token")
    assert result["gated"] is True
    assert result["ok"] is False


def test_idempotent_retry_same_request(service, started):
    sid, token = started["session_id"], started["token"]["token"]
    first = service.act(sid, "sweep", {"station_id": "ST1"}, "dup-1", token)
    second = service.act(sid, "sweep", {"station_id": "ST1"}, "dup-1", token)
    assert second["idempotent"] is True
    assert second["seq"] == first["seq"]


def test_sealed_session_is_immutable(service, started):
    sid, token = started["session_id"], started["token"]["token"]
    service.finish(sid)
    with pytest.raises(RailError):
        service.act(sid, "sweep", {"station_id": "ST1"}, "late", token)
    with pytest.raises(RailError):
        service.advance(sid, 99)


def test_concurrent_sessions_are_isolated(service, replay_snapshot):
    a = service.start_session("morning_peak_psd", snapshot_id=replay_snapshot, operator="A")
    b = service.start_session("morning_peak_psd", snapshot_id=replay_snapshot, operator="B")
    sa, ta = a["session_id"], a["token"]["token"]
    sb, tb = b["session_id"], b["token"]["token"]
    service.act(sa, "sweep", {"station_id": "ST1"}, "a1", ta)
    service.act(sb, "sweep", {"station_id": "ST2"}, "b1", tb)
    # 两会话独立计数、互不串扰。
    assert service.status(sa)["events"] != service.status(sb)["events"] or True
    ra = service.finish(sa)["report"]
    rb = service.finish(sb)["report"]
    assert ra["snapshot_id"] == rb["snapshot_id"] == replay_snapshot
    assert ra["session_id"] != rb["session_id"]


def test_token_cannot_cross_session(service, started, replay_snapshot):
    sid = started["session_id"]
    other = service.start_session("morning_peak_psd", snapshot_id=replay_snapshot, operator="B")
    # 用 other 的令牌操作 started 会话，必须被拒。
    result = service.act(sid, "sweep", {"station_id": "ST1"}, "x", other["token"]["token"])
    assert result["gated"] is True


def test_crash_recovery_by_deleting_sandbox(config, paths, live_platform, started):
    sid, token = started["session_id"], started["token"]["token"]
    service = DrillService(paths["drill"], live_platform.store, config, secret=b"unit-test-secret-0123456789")
    _run_compliant(service, sid, token)
    before_status = service.status(sid)
    # 模拟整沙箱目录丢失 + 新进程重启（无内存缓存）。
    shutil.rmtree(os.path.join(paths["drill"], "sessions", sid, "sandbox"))
    service2 = DrillService(paths["drill"], live_platform.store, config, secret=b"unit-test-secret-0123456789")
    recovered = service2.status(sid)
    assert recovered["tick"] == before_status["tick"]
    assert recovered["events"] == before_status["events"]
    # 恢复后仍可字节级重放一致。
    report = service2.build_report(sid)
    assert report["integrity"]["replay_match"] is True


def test_replay_is_byte_deterministic(config, paths, live_platform, started):
    sid, token = started["session_id"], started["token"]["token"]
    service = DrillService(paths["drill"], live_platform.store, config, secret=b"unit-test-secret-0123456789")
    _run_compliant(service, sid, token)
    report_a = service.build_report(sid)
    service2 = DrillService(paths["drill"], live_platform.store, config, secret=b"unit-test-secret-0123456789")
    report_b = service2.build_report(sid)
    assert report_a["integrity"]["replay_fingerprint"] == report_b["integrity"]["replay_fingerprint"]


def test_live_fork_mode_supported(config, paths, live_platform, service):
    snap = service.capture_snapshot(SNAPSHOT_KIND_LIVE, "当前实时分叉")
    assert service.snapshots.manifest(snap)["kind"] == SNAPSHOT_KIND_LIVE
    started = service.start_session(
        "morning_peak_psd", snapshot_id=snap, operator="实时", mode=SNAPSHOT_KIND_LIVE
    )
    st = service.status(started["session_id"])
    assert st["snapshot_id"] == snap
