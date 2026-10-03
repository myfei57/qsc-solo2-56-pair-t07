"""应急演练子系统测试夹具。"""

from __future__ import annotations

import os

import pytest

from psdline.app import build_platform
from psdline.config import default_config
from psdline.drill.service import DrillService
from psdline.drill.sandbox import fingerprint_store
from psdline.drill.snapshot import SNAPSHOT_KIND_LIVE, SNAPSHOT_KIND_REPLAY


@pytest.fixture()
def config():
    return default_config()


@pytest.fixture()
def paths(tmp_path):
    return {
        "live": str(tmp_path / "live"),
        "drill": str(tmp_path / "drill"),
    }


@pytest.fixture()
def live_platform(config, paths):
    platform = build_platform(config, storage_root=paths["live"])
    platform.start()
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    return platform


@pytest.fixture()
def service(config, paths, live_platform):
    return DrillService(paths["drill"], live_platform.store, config, secret=b"unit-test-secret-0123456789")


@pytest.fixture()
def replay_snapshot(service):
    return service.capture_snapshot(SNAPSHOT_KIND_REPLAY, "故障时刻")


@pytest.fixture()
def started(service, replay_snapshot):
    result = service.start_session(
        "morning_peak_psd", snapshot_id=replay_snapshot, operator="测试员"
    )
    return result
