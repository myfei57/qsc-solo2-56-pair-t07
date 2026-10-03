"""快照：只读捕获、内容寻址幂等、不可写回现场、完整性校验。"""

from __future__ import annotations

import json

import pytest

from psdline.drill.canonical import digest
from psdline.drill.sandbox import fingerprint_store
from psdline.drill.snapshot import SnapshotError, SnapshotStore, SNAPSHOT_KIND_REPLAY


def test_capture_is_readonly(live_platform, paths):
    before = fingerprint_store(live_platform.store)
    store = SnapshotStore(paths["drill"])
    snap = store.capture(
        live_platform.store,
        kind=SNAPSHOT_KIND_REPLAY,
        label="x",
        captured_at=1.0,
    )
    after = fingerprint_store(live_platform.store)
    assert after["fingerprint"] == before["fingerprint"]
    assert store.exists(snap)


def test_snapshot_idempotent_same_id(live_platform, paths):
    store = SnapshotStore(paths["drill"])
    a = store.capture(live_platform.store, kind=SNAPSHOT_KIND_REPLAY, label="a", captured_at=1.0)
    b = store.capture(live_platform.store, kind=SNAPSHOT_KIND_REPLAY, label="b", captured_at=2.0)
    # 身份只由内容决定，时间/标签不同仍是同一快照。
    assert a == b


def test_cannot_restore_into_live_root(live_platform, paths, replay_snapshot, service):
    # 现场根既不在 sessions/ 子树，也等于快照来源根，双重拒绝。
    with pytest.raises(SnapshotError):
        service.snapshots.restore_into(replay_snapshot, live_platform.store)


def test_tamper_is_detected(live_platform, paths, replay_snapshot):
    store = SnapshotStore(paths["drill"])
    manifest_path = f"{paths['drill']}/snapshots/{replay_snapshot}/manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["label"] = "tampered"
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    with pytest.raises(SnapshotError):
        store.verify(replay_snapshot)


def test_doc_tamper_is_detected(service, live_platform, paths, replay_snapshot):
    store = service.snapshots
    manifest = store.manifest(replay_snapshot)
    doc_id = manifest["docs"][0]["doc_id"]
    target = f"{paths['drill']}/snapshots/{replay_snapshot}/docs/{doc_id}.json"
    with open(target, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if isinstance(value, dict):
        value["__tampered__"] = True
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
    with pytest.raises(SnapshotError):
        store.verify(replay_snapshot)
