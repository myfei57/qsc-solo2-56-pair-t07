"""演练审计：与现场分离、哈希链、封存 HMAC、篡改可验。"""

from __future__ import annotations

import json

import pytest

from psdline.drill.audit import DrillAuditError, DrillAuditLog


def _audit(tmp_path, secret=b"audit-secret-0123456789ab"):
    return DrillAuditLog(str(tmp_path), secret)


def test_chain_and_seal(tmp_path):
    audit = _audit(tmp_path)
    audit.record("session_begin", "drill-1", operator="x")
    audit.seal_session("drill-1", {"events": 1, "status": "ended", "score": 6})
    result = audit.verify()
    assert result["ok"] is True
    assert result["seals"] == 1


def test_sealed_session_rejects_more_records(tmp_path):
    audit = _audit(tmp_path)
    audit.record("session_begin", "drill-1")
    audit.seal_session("drill-1", {})
    with pytest.raises(DrillAuditError):
        audit.record("operator_action", "drill-1")
    # 其它会话仍可写。
    audit.record("session_begin", "drill-2")


def test_tamper_after_seal_detected(tmp_path):
    audit = _audit(tmp_path)
    audit.record("session_begin", "drill-1", x=1)
    audit.seal_session("drill-1", {})
    path = audit.path
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    first = json.loads(lines[0])
    first["detail"] = {"x": 2}
    lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    reopened = _audit(tmp_path)
    # 启动恢复会裁掉失配后缀；复核应发现链不可信（记录被截断）。
    assert reopened.verify()["ok"] is False or len(reopened.list()) < 1


def test_audit_file_separate_from_live(tmp_path):
    audit = _audit(tmp_path)
    audit.record("session_begin", "drill-1")
    assert "drill-audit.jsonl" in __import__("os").listdir(str(tmp_path))
