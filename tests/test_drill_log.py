"""哈希链日志：追加、校验、撕裂恢复、终态只读、篡改必被发现。"""

from __future__ import annotations

import json
import os

import pytest

from psdline.clock import ManualClock
from psdline.drill.crypto import GENESIS_HASH
from psdline.drill.errors import LogIntegrityError
from psdline.drill.log import DrillEventLog, iter_paired, normalize_result


def test_chain_appends_and_verifies(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock())
    log.append("action", {"a": 1}, "u1")
    log.append("tick", {"dt": 1}, "system")
    log.append("state", {"phase": "seal"}, "u1")
    assert log.count == 3
    assert log.verify() is None
    # 每条都链接前一条
    records = log.records()
    assert records[0].prev_hash == GENESIS_HASH
    assert records[1].prev_hash == records[0].hash
    assert records[2].prev_hash == records[1].hash
    assert log.tip_hash == records[2].hash


def test_reopen_recomputes_chain(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock(100.0))
    log.append("action", {"x": 1})
    log.append("action", {"x": 2})
    tip = log.tip_hash
    reopened = DrillEventLog(str(tmp_path), ManualClock(100.0))
    assert reopened.count == 2
    assert reopened.tip_hash == tip
    assert reopened.verify() is None


def test_torn_final_line_is_truncated(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock())
    log.append("action", {"x": 1})
    good = log.append("action", {"x": 2})
    # 模拟掉电：末行只写了一半（非合法 JSON）
    with open(log.path, "a", encoding="utf-8") as handle:
        handle.write('{"seq":3,"sim_ti')
        handle.flush()
        os.fsync(handle.fileno())
    reopened = DrillEventLog(str(tmp_path), ManualClock())
    assert reopened.count == 2  # 撕裂行被截掉，完整记录一条不丢
    assert reopened.tip_hash == good.hash


def test_tampered_payload_breaks_chain(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock())
    log.append("action", {"amount": 100})
    log.append("action", {"amount": 200})
    path = log.path
    lines = open(path, encoding="utf-8").read().splitlines()
    first = json.loads(lines[0])
    first["payload"]["amount"] = 999  # 篡改历史
    lines[0] = json.dumps(first, ensure_ascii=False)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    # 篡改在重新打开时即被 fail-fast 拦下（不等显式 verify）
    with pytest.raises(LogIntegrityError):
        DrillEventLog(str(tmp_path), ManualClock())


def test_reordering_events_breaks_chain(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock())
    log.append("action", {"n": 1})
    log.append("action", {"n": 2})
    path = log.path
    lines = open(path, encoding="utf-8").read().splitlines()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(reversed(lines)) + "\n")
    with pytest.raises(LogIntegrityError):
        DrillEventLog(str(tmp_path), ManualClock())


def test_frozen_log_rejects_append(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock())
    log.append("action", {"x": 1})
    log.freeze()
    with pytest.raises(LogIntegrityError):
        log.append("action", {"x": 2})
    # 文件权限只读
    assert not os.access(log.path, os.W_OK)


def test_request_result_pairing_detects_inflight(tmp_path):
    log = DrillEventLog(str(tmp_path), ManualClock())
    log.append("action", {"phase": "request", "request_id": "r1", "action": "depart"})
    log.append("action", {"phase": "result", "request_id": "r1", "result": {"ok": True}})
    log.append("action", {"phase": "request", "request_id": "r2", "action": "sweep"})
    # r2 没有 result：执行中崩溃
    paired = iter_paired(log.records())
    assert paired[0][1] is not None
    assert paired[1][1] is None
    assert paired[1][0]["request_id"] == "r2"


def test_normalize_result_strips_nondeterministic_ids():
    result = {"ok": True, "op_id": "op-abc", "audit_id": "au-1", "violations": []}
    assert normalize_result(result) == {"ok": True, "violations": []}
