"""门控：演练指令绝不触达现场，fail-closed，令牌绑定会话。"""

from __future__ import annotations

import pytest

from psdline.drill.gate import (
    DENY_BAD_TOKEN,
    DENY_SESSION_CLOSED,
    DENY_SESSION_MISMATCH,
    DENY_UNKNOWN_ACTION,
    DRILL,
    LIVE,
    CommandEnvelope,
    CommandGate,
    DrillTokenSigner,
    FieldGateway,
    GateError,
)


def _envelope(action, session_id, token, request_id="req-1", args=None):
    return CommandEnvelope.of(
        {
            "action": action,
            "mode": DRILL,
            "session_id": session_id,
            "token": token,
            "args": args or {},
            "client_request_id": request_id,
            "issued_at": 0.0,
        }
    )


def test_drill_mode_never_transmits():
    signer = DrillTokenSigner(b"x" * 32)
    sid, snap = "drill-a", "snap-1"
    token = signer.issue(sid, snap)["token"]
    field = FieldGateway()
    gate = CommandGate(
        DRILL,
        signer,
        is_session_open=lambda s: s == sid,
        snapshot_of=lambda s: snap if s == sid else "",
    )
    verdict = gate.screen(_envelope("sweep", sid, token, args={"station_id": "ST1"}))
    assert verdict.allowed is True
    assert verdict.transmitted is False
    # 现场出口始终为零：演练门根本拿不到出口。
    assert field.transmitted_count == 0


def test_bad_token_denied():
    signer = DrillTokenSigner(b"x" * 32)
    gate = CommandGate(
        DRILL,
        signer,
        is_session_open=lambda s: True,
        snapshot_of=lambda s: "snap-1",
    )
    verdict = gate.screen(_envelope("sweep", "drill-a", "forged-token"))
    assert verdict.allowed is False
    assert verdict.reason == DENY_BAD_TOKEN
    assert verdict.transmitted is False


def test_token_bound_to_session_cannot_cross():
    signer = DrillTokenSigner(b"x" * 32)
    token = signer.issue("drill-a", "snap-1")["token"]
    gate = CommandGate(
        DRILL,
        signer,
        is_session_open=lambda s: True,
        snapshot_of=lambda s: {"drill-a": "snap-1", "drill-b": "snap-2"}[s],
    )
    verdict = gate.screen(_envelope("sweep", "drill-b", token))
    assert verdict.allowed is False
    assert verdict.reason == DENY_SESSION_MISMATCH


def test_closed_session_denied():
    signer = DrillTokenSigner(b"x" * 32)
    token = signer.issue("drill-a", "snap-1")["token"]
    gate = CommandGate(DRILL, signer, is_session_open=lambda s: False, snapshot_of=lambda s: "snap-1")
    verdict = gate.screen(_envelope("sweep", "drill-a", token))
    assert verdict.allowed is False
    assert verdict.reason == DENY_SESSION_CLOSED


def test_unknown_action_denied():
    signer = DrillTokenSigner(b"x" * 32)
    token = signer.issue("drill-a", "snap-1")["token"]
    gate = CommandGate(DRILL, signer, is_session_open=lambda s: True, snapshot_of=lambda s: "snap-1")
    verdict = gate.screen(_envelope("format_all_devices", "drill-a", token))
    assert verdict.allowed is False
    assert verdict.reason == DENY_UNKNOWN_ACTION


def test_live_mode_rejects_drill_envelope():
    signer = DrillTokenSigner(b"x" * 32)
    gate = CommandGate(LIVE, signer, is_session_open=lambda s: True, snapshot_of=lambda s: "snap-1")
    envelope = _envelope("sweep", "drill-a", signer.issue("drill-a", "snap-1")["token"])
    verdict = gate.screen(envelope)
    # 封套声明的是 drill 模式，现场门不放行，且不触达。
    assert verdict.allowed is False
    assert verdict.transmitted is False


def test_short_secret_rejected():
    with pytest.raises(GateError):
        DrillTokenSigner(b"short")
