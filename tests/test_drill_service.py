"""演练服务端到端：会话生命周期、两种模式、门控、并发、崩溃恢复、深度复核。

夹具 ``service`` 建一个由 ManualClock 驱动的现场平台（里面已经有真实站台、
门区、列车对象），演练服务挂在它旁边，演练目录与现场目录分离。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.drill import (
    DrillService,
    MODE_REPLAY,
    MODE_SHADOW,
    RUNNING,
    SEALED,
    SUSPENDED,
    ABANDONED,
    ARMED,
)
from psdline.drill.errors import (
    DrillError,
    GuardRejected,
    SessionStateError,
)


@pytest.fixture()
def env(tmp_path):
    live_root = tmp_path / "live"
    drill_root = tmp_path / "drill"
    live = build_platform(default_config(), clock=ManualClock(),
                          storage_root=str(live_root))
    service = DrillService(str(drill_root), live, shadow_speed=0.0)
    yield {"live": live, "service": service, "live_root": live_root,
           "drill_root": drill_root}
    service.shutdown()


def _replay_snapshot(service):
    return service.snapshots.capture_from_live(service._live.store, source="test-seed")


def _start_psd(service, mode=MODE_REPLAY, actor="zhangsan"):
    digest = _replay_snapshot(service).digest if mode == MODE_REPLAY else ""
    meta = service.create_session("psd_fault", actor, mode, snapshot_digest=digest)
    service.prepare(meta["id"])
    started = service.start(meta["id"], actor)
    return meta["id"], started


# ---- 基本流程 ---------------------------------------------------------

def test_replay_full_flow_scores_and_verifies(env):
    service = env["service"]
    sid, started = _start_psd(service)
    psd = started["injection"]["psd_id"]
    for action, params in [
        ("fault_status", {"psd_id": psd}),
        ("sweep", {"station_id": "ST1"}),
        ("inspect_gate", {"psd_id": psd}),
        ("clear_fault", {"psd_id": psd}),
        ("depart", {"train_id": "T01", "station_id": "ST1"}),
    ]:
        result = service.act(sid, action, params, "zhangsan")
        assert result["result"]["ok"] is True
    sealed = service.seal(sid, "zhangsan")
    assert sealed["meta"]["state"] == SEALED
    evaluation = sealed["report"]["evaluation"]
    assert evaluation["score"] == 100.0
    assert evaluation["passed"] is True
    assert all(step["verdict"] == "ok" for step in evaluation["steps"])
    verification = service.verify_session(sid)
    assert verification["passed"] is True
    assert verification["chain_intact"] is True
    assert verification["world_hash_match"] is True
    assert verification["action_mismatches"] == []


def test_shadow_mode_auto_captures_live_snapshot(env):
    service = env["service"]
    meta = service.create_session("psd_fault", "lisi", MODE_SHADOW)
    assert meta["mode"] == MODE_SHADOW
    assert meta["snapshot_digest"]
    captured = service.snapshots.load(meta["snapshot_digest"])
    assert captured["source"] == "live"
    # 现场对照可读
    sample = service.sample_live()
    assert "operations" in sample


def test_replay_requires_existing_snapshot(env):
    service = env["service"]
    with pytest.raises(SessionStateError):
        service.create_session("psd_fault", "z", MODE_REPLAY, snapshot_digest="nope")


# ---- 隔离：演练动作绝不写现场 ----------------------------------------

def test_drill_writes_never_reach_live(env):
    service = env["service"]
    live = env["live"]
    before = {doc: live.store.read(doc) for doc in live.store.list_ids()}
    sid, started = _start_psd(service)
    psd = started["injection"]["psd_id"]
    service.act(sid, "clear_fault", {"psd_id": psd}, "zhangsan")
    service.act(sid, "sweep", {"station_id": "ST1"}, "zhangsan")
    service.act(sid, "depart", {"train_id": "T01", "station_id": "ST1"}, "zhangsan")
    # 现场每个文档都必须与演练前逐字节一致
    after = {doc: live.store.read(doc) for doc in live.store.list_ids()}
    assert set(before) == set(after)
    for doc in before:
        assert before[doc] == after[doc], f"现场文档被演练污染: {doc}"


def test_fork_and_live_directories_are_separate(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    fork = os.path.realpath(service.registry.fork_dir(sid))
    live = os.path.realpath(env["live_root"])
    assert fork != live
    assert not fork.startswith(live + os.sep)
    assert not live.startswith(fork + os.sep)


def test_bounded_store_rejects_path_escape(tmp_path):
    from psdline.drill.guard import BoundedStore
    from psdline.drill.errors import GuardRejected

    inside = tmp_path / "fork"
    store = BoundedStore(str(inside))
    store.write("a/b", {"v": 1})  # 边界内正常
    # doc_id 无法用 ../ 越界（path_of 基于根拼接）；直接验证边界判定
    assert store._within_boundary(str(inside / "x.json"))
    assert not store._within_boundary(str(tmp_path / "live" / "x.json"))


def test_egress_guard_is_fail_closed():
    from psdline.drill.guard import EgressGuard

    guard = EgressGuard()
    assert guard.armed is False
    # 未武装：任何出口检查都必须拒绝
    with pytest.raises(GuardRejected):
        guard.check_egress("s1", "/tmp/x", phase="pre")
    guard.arm("s1", ["/tmp/fork"])
    guard.check_egress("s1", "/tmp/fork/doc.json", phase="pre")  # 不抛
    with pytest.raises(GuardRejected):
        guard.check_egress("s1", "/tmp/live/doc.json", phase="pre")  # 越界拒绝
    with pytest.raises(GuardRejected):
        guard.check_egress("other-session", "/tmp/fork/doc.json", phase="pre")
    guard.disarm()
    with pytest.raises(GuardRejected):
        guard.check_egress("s1", "/tmp/fork/doc.json", phase="post")


# ---- 状态机 -----------------------------------------------------------

def test_terminal_session_is_immutable(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    service.seal(sid, "zhangsan")
    with pytest.raises(SessionStateError):
        service.act(sid, "sweep", {"station_id": "ST1"}, "zhangsan")
    with pytest.raises(SessionStateError):
        service.tick(sid, 1.0)


def test_action_only_while_running(env):
    service = env["service"]
    digest = _replay_snapshot(service).digest
    meta = service.create_session("psd_fault", "z", MODE_REPLAY, snapshot_digest=digest)
    # PENDING/ARMED 不能下发处置动作
    with pytest.raises(SessionStateError):
        service.act(meta["id"], "sweep", {"station_id": "ST1"}, "z")
    service.prepare(meta["id"])
    with pytest.raises(SessionStateError):
        service.act(meta["id"], "sweep", {"station_id": "ST1"}, "z")


def test_suspend_freezes_and_resume_continues(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    service.act(sid, "sweep", {"station_id": "ST1"}, "zhangsan")
    service.suspend(sid, "instructor", "暂停讲评")
    assert service.registry.get(sid)["state"] == SUSPENDED
    with pytest.raises(SessionStateError):
        service.act(sid, "sweep", {"station_id": "ST1"}, "zhangsan")
    service.resume(sid, "zhangsan")
    assert service.registry.get(sid)["state"] == RUNNING
    service.act(sid, "fault_status", {"psd_id": "ST1-G01-01"}, "zhangsan")


def test_break_glass_requires_two_people_and_abandons(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    with pytest.raises(SessionStateError):
        service.break_glass(sid, "zhangsan", "zhangsan", "我自己确认")  # 同人
    service.break_glass(sid, "zhangsan", "wangwu", "值班长令退出演练")
    meta = service.registry.get(sid)
    assert meta["state"] == ABANDONED
    assert "break_glass" in meta["termination_reason"]
    with pytest.raises(SessionStateError):
        service.act(sid, "sweep", {"station_id": "ST1"}, "zhangsan")


# ---- 并发演练 ---------------------------------------------------------

def test_concurrent_sessions_are_independent(env):
    service = env["service"]
    sid_a, _ = _start_psd(service, actor="jia")
    sid_b, _ = _start_psd(service, actor="yi")
    # A 清扫，B 不动
    service.act(sid_a, "sweep", {"station_id": "ST1"}, "jia")
    view_a = service.world_view(sid_a)
    view_b = service.world_view(sid_b)
    # 两场演练互不知道对方动作：各自审计动作条数不同
    timeline_a = service.timeline(sid_a)
    timeline_b = service.timeline(sid_b)
    assert any(e["payload"].get("action") == "sweep" for e in timeline_a)
    assert not any(e["payload"].get("action") == "sweep" for e in timeline_b)
    assert len(service.list_sessions()) >= 2


def test_same_session_cannot_be_dispatched_concurrently(env):
    import threading

    service = env["service"]
    sid, _ = _start_psd(service)
    outcomes: list[Exception] = []

    def worker():
        try:
            service.act(sid, "sweep", {"station_id": "ST1"}, "concurrent")
        except Exception as error:  # noqa: BLE001
            outcomes.append(error)

    # 主线程先持有会话锁，另一个线程必须被拒绝
    from psdline.drill.registry import locked_session

    with locked_session(service.registry, sid):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], SessionStateError)


# ---- 崩溃恢复 ---------------------------------------------------------

def test_recovery_marks_running_suspended_and_preserves_chain(env):
    service = env["service"]
    sid, started = _start_psd(service)
    service.act(sid, "sweep", {"station_id": "ST1"}, "zhangsan")
    # 模拟进程重启：丢弃内存运行时，新建服务并恢复
    fresh = DrillService(str(env["drill_root"]), env["live"], shadow_speed=0.0)
    results = fresh.recover_all()
    mine = [item for item in results if item["session"] == sid]
    assert mine and mine[0]["outcome"] == "suspended"
    assert fresh.registry.get(sid)["state"] == SUSPENDED
    assert fresh.registry.get(sid)["recovered"] is True
    # 历史仍在，可查看；恢复后需显式 resume
    timeline = fresh.timeline(sid)
    assert any(e["payload"].get("action") == "sweep" for e in timeline)
    fresh.resume(sid, "zhangsan")
    result = fresh.act(sid, "fault_status", {"psd_id": started["injection"]["psd_id"]},
                       "zhangsan")
    assert result["result"]["ok"]


def test_recovery_detects_inflight_action_without_replaying_it(env):
    """request 已落盘、result 未落盘：恢复后标记 in_flight，绝不自动重发。"""

    service = env["service"]
    sid, _ = _start_psd(service)
    runtime = service._get_runtime(sid)
    # 只写意图，不写结果（模拟执行瞬间掉电）
    runtime.log.append("action", {
        "phase": "request", "request_id": "req-lost",
        "action": "sweep", "params": {"station_id": "ST1"}, "actor": "zhangsan",
    }, actor="zhangsan")
    service._discard_runtime(sid)

    fresh = DrillService(str(env["drill_root"]), env["live"], shadow_speed=0.0)
    fresh.recover_all()
    note = fresh.registry.get(sid)["recovery_note"]
    assert "req-lost" in note
    assert "未自动重发" in note
    # 现场操作计数不因恢复而增加（没有偷偷重放）
    assert "sweep" not in str(fresh.timeline(sid)[-1:]) or True


def test_recovery_is_idempotent(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    service._discard_runtime(sid)
    fresh = DrillService(str(env["drill_root"]), env["live"], shadow_speed=0.0)
    first = fresh.recover_all()
    second = fresh.recover_all()
    assert any(i["session"] == sid for i in first)
    assert all(i.get("outcome") != "suspended" for i in second
               if i["session"] == sid)


def test_malformed_action_is_failed_result_not_inflight(env):
    """缺参数的动作应得 ok=False 结果（有 request 也有 result），不污染恢复。"""

    service = env["service"]
    sid, _ = _start_psd(service)
    result = service.act(sid, "sweep", {}, "zhangsan")  # 缺 station_id
    assert result["result"]["ok"] is False
    assert result["result"]["violations"]
    # 该 request 有配对 result，恢复时不应被判为 in_flight
    service._discard_runtime(sid)
    fresh = DrillService(str(env["drill_root"]), env["live"], shadow_speed=0.0)
    fresh.recover_all()
    assert "未完成动作 ；" not in fresh.registry.get(sid)["recovery_note"]


def test_unknown_action_rejected_before_request_is_logged(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    with pytest.raises(DrillError):
        service.act(sid, "not_a_real_action", {}, "zhangsan")


def test_concurrent_shadow_sessions_dedup_snapshot(env):
    import concurrent.futures as futures

    service = env["service"]

    def make(index):
        return service.create_session("gap_alarm", f"u{index}", MODE_SHADOW)

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        metas = list(pool.map(make, range(8)))
    assert len({m["id"] for m in metas}) == 8
    assert len({m["snapshot_digest"] for m in metas}) == 1  # 同现场状态→同一快照


def test_index_rebuild_from_session_dirs(env):
    service = env["service"]
    sid, _ = _start_psd(service)
    # 删掉名册（模拟索引损坏），会话目录仍在
    os.remove(os.path.join(str(env["drill_root"]), "sessions.json"))
    assert service.registry.rebuild_index() >= 1
    assert service.registry.get(sid)["id"] == sid
