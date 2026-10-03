"""快照分叉与会话名册：内容寻址去重、物理隔离、名册重建、状态机、跨进程锁。"""

from __future__ import annotations

import json
import os
import threading

import pytest

from psdline.clock import ManualClock
from psdline.drill.fork import SnapshotStore, world_fingerprint
from psdline.drill.errors import SessionStateError
from psdline.drill.model import can_transition, RUNNING, SEALED, SUSPENDED
from psdline.drill.registry import (
    SessionRegistry,
    locked_session,
)
from psdline.store import JsonStore


@pytest.fixture()
def live_store(tmp_path):
    store = JsonStore(str(tmp_path / "live"))
    store.write("station/ST1/layout", {"zones": ["G01"]})
    store.write("platform/seed", {"line": "L1"})
    return store


def test_snapshot_is_content_addressed_and_dedup(live_store, tmp_path):
    snaps = SnapshotStore(str(tmp_path / "drill"), ManualClock())
    first = snaps.capture_from_live(live_store, source="live")
    second = snaps.capture_from_live(live_store, source="live")
    assert first.digest == second.digest  # 同内容同指纹，去重复用
    bundle = snaps.load(first.digest)
    assert bundle["source"] == "live"
    assert "station/ST1/layout" in bundle["documents"]
    assert len(snaps.list()) == 1


def test_snapshot_changes_when_live_changes(live_store, tmp_path):
    snaps = SnapshotStore(str(tmp_path / "drill"), ManualClock())
    first = snaps.capture_from_live(live_store, source="live")
    live_store.write("station/ST1/layout", {"zones": ["G01", "G02"]})
    second = snaps.capture_from_live(live_store, source="live")
    assert first.digest != second.digest  # 一个字节变化都换指纹


def test_snapshot_bundle_is_readonly(live_store, tmp_path):
    snaps = SnapshotStore(str(tmp_path / "drill"), ManualClock())
    bundle = snaps.capture_from_live(live_store, source="live")
    assert not os.access(bundle.path, os.W_OK)


def test_fork_is_independent_writable_copy(live_store, tmp_path):
    snaps = SnapshotStore(str(tmp_path / "drill"), ManualClock())
    bundle = snaps.capture_from_live(live_store, source="live")
    fork_root = str(tmp_path / "fork")
    snaps.fork_to(bundle.digest, fork_root)
    fork = JsonStore(fork_root)
    # 在副本上写，不影响快照与现场
    fork.write("drill-only", {"x": 1})
    assert fork.read("drill-only") == {"x": 1}
    assert live_store.read("drill-only") is None
    untouched = snaps.load(bundle.digest)
    assert "drill-only" not in untouched["documents"]


def test_world_fingerprint_stable_for_same_business_state(tmp_path):
    store_a = JsonStore(str(tmp_path / "a"))
    store_b = JsonStore(str(tmp_path / "b"))
    for store in (store_a, store_b):
        store.write("doc1", {"v": 1})
        store.write("doc2", {"v": 2})
    assert world_fingerprint(store_a) == world_fingerprint(store_b)
    store_b.write("doc2", {"v": 3})
    assert world_fingerprint(store_a) != world_fingerprint(store_b)


# ---- 名册与状态机 -----------------------------------------------------

def test_registry_create_and_state_machine(tmp_path):
    registry = SessionRegistry(str(tmp_path / "drill"))
    meta = registry.create(mode="replay", scenario="psd_fault", actor="z")
    sid = meta["id"]
    assert meta["state"] == "pending"
    assert registry.get(sid)["id"] == sid
    registry.transition(sid, "armed")
    registry.transition(sid, "running")
    assert registry.get(sid)["state"] == "running"
    registry.transition(sid, "sealed")
    with pytest.raises(SessionStateError):
        registry.transition(sid, "running")  # 终态不可逆


def test_illegal_transition_rejected(tmp_path):
    registry = SessionRegistry(str(tmp_path / "drill"))
    meta = registry.create(mode="shadow", scenario="x", actor="z")
    with pytest.raises(SessionStateError):
        registry.transition(meta["id"], "sealed")  # pending 不能直接封存
    assert can_transition("running", "suspended") is True
    assert can_transition("sealed", "running") is False


def test_unknown_session_raises(tmp_path):
    from psdline.drill.errors import SessionNotFoundError

    registry = SessionRegistry(str(tmp_path / "drill"))
    with pytest.raises(SessionNotFoundError):
        registry.get("DR-nope")


def test_rebuild_index_after_loss(tmp_path):
    registry = SessionRegistry(str(tmp_path / "drill"))
    ids = []
    for _ in range(3):
        ids.append(registry.create(mode="replay", scenario="psd_fault", actor="z")["id"])
    os.remove(os.path.join(str(tmp_path / "drill"), "sessions.json"))
    assert registry.rebuild_index() == 3
    for sid in ids:
        assert registry.get(sid)["id"] == sid


def test_session_lock_excludes_concurrent_users(tmp_path):
    registry = SessionRegistry(str(tmp_path / "drill"))
    meta = registry.create(mode="replay", scenario="psd_fault", actor="z")
    sid = meta["id"]
    holder_result = {}

    def hold():
        lock = registry.session_lock(sid)
        holder_result["acquired"] = lock.acquire()
        import time as _time

        _time.sleep(0.4)
        lock.release()

    thread = threading.Thread(target=hold)
    thread.start()
    import time

    time.sleep(0.1)
    # 第二个执行者非阻塞获取同一把锁必须失败
    other = registry.session_lock(sid)
    assert other.acquire() is False
    thread.join()
    # 持有者释放后可以再获取
    again = registry.session_lock(sid)
    assert again.acquire() is True
    again.release()


def test_locked_session_context_blocks(tmp_path):
    registry = SessionRegistry(str(tmp_path / "drill"))
    meta = registry.create(mode="replay", scenario="psd_fault", actor="z")
    sid = meta["id"]
    with locked_session(registry, sid):
        with pytest.raises(SessionStateError):
            with locked_session(registry, sid):
                pass
