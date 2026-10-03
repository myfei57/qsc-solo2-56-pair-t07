"""哈希链事件流：历史不可变、撕裂尾裁剪、篡改断链可验。"""

from __future__ import annotations

import os

from psdline.drill.eventlog import ChainedEventLog


def test_hash_chain_links(tmp_path):
    log = ChainedEventLog(str(tmp_path))
    e0 = log.append("session_begin", 1.0, 0, {"a": 1})
    e1 = log.append("operator_action", 2.0, 1, {"b": 2})
    assert e0.prev_hash == ""
    assert e1.prev_hash == e0.this_hash
    assert log.verify_chain()["ok"] is True


def test_unterminated_tail_is_truncated(tmp_path):
    log = ChainedEventLog(str(tmp_path))
    log.append("session_begin", 1.0, 0, {"a": 1})
    log.append("operator_action", 2.0, 1, {"b": 2})
    # 模拟崩溃：追加一行没有结尾换行的半成品。
    with open(log.path, "a", encoding="utf-8") as handle:
        handle.write('{"seq":2,"kind":"note"')
    reopened = ChainedEventLog(str(tmp_path))
    assert reopened.next_seq == 2
    assert any(item["reason"] == "unterminated_tail" for item in reopened.truncated_tail)
    # 裁剪后链仍可继续追加且自洽。
    reopened.append("note", 3.0, 1, {"text": "ok"})
    assert reopened.verify_chain()["ok"] is True


def test_tampered_record_breaks_chain(tmp_path):
    log = ChainedEventLog(str(tmp_path))
    log.append("session_begin", 1.0, 0, {"a": 1})
    log.append("operator_action", 2.0, 1, {"b": 2})
    log.append("note", 3.0, 1, {"text": "orig"})
    # 改写中间一行的负载，重开时应从失配点裁掉，链顶回到可信前缀。
    with open(log.path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    import json

    middle = json.loads(lines[1])
    middle["payload"] = {"b": 999}
    lines[1] = json.dumps(middle, ensure_ascii=False, sort_keys=True) + "\n"
    with open(log.path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    reopened = ChainedEventLog(str(tmp_path))
    # 第 1 条被改，其后全部不可信 -> 只剩 seq 0。
    assert reopened.next_seq == 1
    assert reopened.verify_chain()["ok"] is True
    assert any(item["reason"] == "hash_mismatch" for item in reopened.truncated_tail)
