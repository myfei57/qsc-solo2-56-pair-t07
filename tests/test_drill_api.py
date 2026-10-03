"""演练 HTTP API：路由委托、状态码映射、现场域不被污染。"""

from __future__ import annotations

import json

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.console.api import Api as LiveApi
from psdline.drill import DrillService, MODE_REPLAY, MODE_SHADOW
from psdline.drill.api import DrillApi


@pytest.fixture()
def wired(tmp_path):
    live = build_platform(default_config(), clock=ManualClock(),
                          storage_root=str(tmp_path / "live"))
    service = DrillService(str(tmp_path / "drill"), live, shadow_speed=0.0)
    drill_api = DrillApi(service)
    live_api = LiveApi(live, drill_api)
    return live, service, drill_api, live_api


def _create_running(service, mode=MODE_REPLAY, actor="z", scenario="psd_fault"):
    digest = ""
    if mode == MODE_REPLAY:
        digest = service.snapshots.capture_from_live(service._live.store, source="t").digest
    meta = service.create_session(scenario, actor, mode, snapshot_digest=digest)
    service.prepare(meta["id"])
    service.start(meta["id"], actor)
    return meta["id"]


def test_drill_routes_present(wired):
    _live, _service, drill_api, _live_api = wired
    paths = [route["path"] for route in drill_api.routes()]
    assert "/api/drill/sessions" in paths
    assert "/api/drill/sessions/{sessionID}/act" in paths
    assert "/api/drill/sessions/{sessionID}/verify" in paths


def test_scenarios_listing(wired):
    _live, _service, _d, live_api = wired
    status, payload = live_api.handle("GET", "/api/drill/scenarios", {}, {})
    assert status == 200
    assert {s["key"] for s in payload["scenarios"]} == {
        "psd_fault", "gap_alarm", "stale_position"}


def test_full_session_lifecycle_over_api(wired):
    _live, service, _d, api = wired
    status, created = api.handle("POST", "/api/drill/sessions", {},
                                 {"scenario": "psd_fault", "mode": MODE_SHADOW, "actor": "z"})
    assert status == 200
    sid = created["id"]
    assert api.handle("POST", f"/api/drill/sessions/{sid}/prepare", {}, {})[0] == 200
    assert api.handle("POST", f"/api/drill/sessions/{sid}/start", {}, {"actor": "z"})[0] == 200

    status, acted = api.handle("POST", f"/api/drill/sessions/{sid}/act", {}, {
        "action": "sweep", "params": {"station_id": "ST1"}, "actor": "z"})
    assert status == 200 and acted["result"]["ok"] is True

    status, sealed = api.handle("POST", f"/api/drill/sessions/{sid}/seal", {}, {"actor": "z"})
    assert status == 200
    assert sealed["report"]["evaluation"]["passed"] is False  # 没按标准做全

    status, verification = api.handle("POST", f"/api/drill/sessions/{sid}/verify", {}, {})
    assert status == 200 and verification["passed"] is True  # 历史本身仍自洽


def test_replay_without_snapshot_is_409(wired):
    _live, _s, _d, api = wired
    status, payload = api.handle("POST", "/api/drill/sessions", {}, {
        "scenario": "psd_fault", "mode": MODE_REPLAY, "actor": "z",
        "snapshot_digest": "deadbeef"})
    assert status == 409
    assert payload["error"] == "session_state"


def test_act_on_pending_session_is_409(wired):
    _live, service, _d, api = wired
    digest = service.snapshots.capture_from_live(_live.store, source="t").digest
    _, meta = api.handle("POST", "/api/drill/sessions", {}, {
        "scenario": "psd_fault", "mode": MODE_REPLAY, "actor": "z",
        "snapshot_digest": digest})
    status, payload = api.handle("POST", f"/api/drill/sessions/{meta['id']}/act", {}, {
        "action": "sweep", "params": {"station_id": "ST1"}})
    assert status == 409


def test_missing_action_field_is_400(wired):
    _live, service, _d, api = wired
    sid = _create_running(service)
    status, payload = api.handle("POST", f"/api/drill/sessions/{sid}/act", {}, {
        "params": {"station_id": "ST1"}})
    assert status == 400
    assert payload["error"] == "bad_drill_request"


def test_unknown_session_is_404(wired):
    _live, _s, _d, api = wired
    status, payload = api.handle("GET", "/api/drill/sessions/DR-nope/world", {}, {})
    assert status == 404
    assert payload["error"] == "session_not_found"


def test_drill_paths_404_when_drill_disabled(tmp_path):
    live = build_platform(default_config(), clock=ManualClock(),
                          storage_root=str(tmp_path / "live"))
    api = LiveApi(live)  # 不挂 drill
    status, payload = api.handle("GET", "/api/drill/scenarios", {}, {})
    assert status == 404
    assert payload["error"] == "drill_disabled"
    # 现场接口不受影响
    assert api.handle("GET", "/api/status", {}, {})[0] == 200


def test_live_audit_has_no_drill_actions(wired):
    """演练下发动作后，现场审计里不得出现任何处置动作。"""

    live, service, _d, api = wired
    sid = _create_running(service)
    api.handle("POST", f"/api/drill/sessions/{sid}/act", {}, {
        "action": "clear_fault", "params": {"psd_id": "ST1-G01-01"}, "actor": "z"})
    kinds = {entry.kind for entry in live.audit.list()}
    # 现场审计只能有平台自身启动/seed 相关，绝不含演练的 clear/arrive 等
    assert "interlock_clear" not in kinds
    assert "door_enqueue" not in kinds
