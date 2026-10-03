"""演练全局审计流：与现场审计物理分离、只追加、哈希链、封存可验。

现场审计写在现场存储根的 ``audit-*.jsonl``；演练审计写在**独立演练根**下的单一
``drill-audit.jsonl``，二者目录不重叠，因此演练痕迹不可能混进现场事实。

与事件流水的区别：

* 事件流水是「单个会话怎么演进」的事实源，用于重放沙箱；
* 全局演练审计是「演练服务这个系统发生过什么」的跨会话合规记录，包含会话
  建立/中止/结束、门控拦截、快照捕获、越权尝试、恢复裁剪等。

记录逐条哈希链；会话封存时写一条 ``session_seal`` 记录，其 HMAC 收尾签名另存到
只追加的 ``seals.jsonl``，供外部独立核验。链的连续哈希本身就保证封存点之后若被
插入/改写，复核时立刻暴露——不存在“删除”接口。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from dataclasses import dataclass

from ..errors import RailError
from .canonical import digest

AUDIT_VERSION = 1
GENESIS = "GENESIS"


class DrillAuditError(RailError):
    """演练审计读写或校验失败。"""


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    at: float
    kind: str
    session_id: str
    detail: dict
    prev_hash: str
    this_hash: str

    def as_dict(self) -> dict:
        return {
            "version": AUDIT_VERSION,
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "session_id": self.session_id,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "this_hash": self.this_hash,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "AuditRecord":
        return cls(
            seq=int(raw["seq"]),
            at=float(raw.get("at", 0.0)),
            kind=str(raw["kind"]),
            session_id=str(raw.get("session_id", "")),
            detail=dict(raw.get("detail", {})),
            prev_hash=str(raw.get("prev_hash", "")),
            this_hash=str(raw.get("this_hash", "")),
        )


def record_hash(seq: int, at: float, kind: str, session_id: str, detail: dict, prev_hash: str) -> str:
    return digest(
        {
            "version": AUDIT_VERSION,
            "seq": seq,
            "at": at,
            "kind": kind,
            "session_id": session_id,
            "detail": detail,
            "prev_hash": prev_hash,
        }
    )


class DrillAuditLog:
    """跨会话的演练审计流水。"""

    def __init__(self, drill_root: str, secret: bytes) -> None:
        self._root = os.path.abspath(drill_root)
        os.makedirs(self._root, exist_ok=True)
        self._path = os.path.join(self._root, "drill-audit.jsonl")
        self._seals_path = os.path.join(self._root, "seals.jsonl")
        self._secret = secret
        self._records: list[AuditRecord] = []
        self._sealed_sessions: set[str] = set()
        self._load()

    @property
    def path(self) -> str:
        return self._path

    def record(self, kind: str, session_id: str = "", **detail) -> AuditRecord:
        """追加一条跨会话审计记录。已封存会话拒绝再写。"""

        if session_id and session_id in self._sealed_sessions:
            raise DrillAuditError(f"会话已封存，拒绝追加审计: {session_id}", component="drill.audit")
        seq = len(self._records)
        prev = self._records[-1].this_hash if self._records else GENESIS
        at = time.time()
        clean = {key: value for key, value in detail.items() if value is not None}
        this_hash = record_hash(seq, at, kind, session_id, clean, prev)
        rec = AuditRecord(seq, at, kind, session_id, clean, prev, this_hash)
        self._append(self._path, rec.as_dict())
        self._records.append(rec)
        return rec

    def seal_session(self, session_id: str, summary: dict) -> dict:
        """封存会话：链上写 seal 记录，并把 HMAC 收尾签名追加到 seals.jsonl。"""

        rec = self.record("session_seal", session_id, summary=summary)
        self._sealed_sessions.add(session_id)
        seal_mac = hmac.new(
            self._secret, rec.this_hash.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        marker = {
            "seal_for": session_id,
            "head_seq": rec.seq,
            "head_hash": rec.this_hash,
            "hmac": seal_mac,
            "at": time.time(),
        }
        self._append(self._seals_path, marker)
        return marker

    def is_sealed(self, session_id: str) -> bool:
        return session_id in self._sealed_sessions

    def list(self, session_id: str | None = None) -> list[AuditRecord]:
        if session_id is None:
            return list(self._records)
        return [rec for rec in self._records if rec.session_id == session_id]

    def verify(self) -> dict:
        """复核链完整性与全部封存 HMAC。"""

        result = {"ok": True, "records": 0, "seals": 0, "problems": []}
        prev = GENESIS
        for rec in self._iter_records():
            if rec.seq != result["records"] or rec.prev_hash != prev:
                result["ok"] = False
                result["problems"].append(f"broken_chain@seq{rec.seq}")
                return result
            if record_hash(rec.seq, rec.at, rec.kind, rec.session_id, rec.detail, rec.prev_hash) != rec.this_hash:
                result["ok"] = False
                result["problems"].append(f"hash_mismatch@seq{rec.seq}")
                return result
            prev = rec.this_hash
            result["records"] += 1
        if os.path.isfile(self._seals_path):
            with open(self._seals_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    marker = json.loads(line)
                    expected = hmac.new(
                        self._secret,
                        str(marker["head_hash"]).encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    if not hmac.compare_digest(expected, str(marker["hmac"])):
                        result["ok"] = False
                        result["problems"].append(f"bad_seal_hmac@{marker.get('seal_for')}")
                    result["seals"] += 1
        return result

    # ---- 内部 ---------------------------------------------------------

    def _append(self, path: str, obj: dict) -> None:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise DrillAuditError(f"写演练审计失败: {error}", component="drill.audit") from error

    def _iter_records(self):
        if not os.path.isfile(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield AuditRecord.from_dict(json.loads(line))

    def _load(self) -> None:
        """启动恢复：顺链读到第一个失配点，撕裂尾裁掉，保证可继续追加。"""

        if not os.path.isfile(self._path):
            return
        kept_lines: list[str] = []
        kept_records: list[AuditRecord] = []
        prev = GENESIS
        with open(self._path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = AuditRecord.from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError):
                break
            if rec.seq != len(kept_records) or rec.prev_hash != prev:
                break
            if record_hash(rec.seq, rec.at, rec.kind, rec.session_id, rec.detail, rec.prev_hash) != rec.this_hash:
                break
            kept_records.append(rec)
            kept_lines.append(line if line.endswith("\n") else line + "\n")
            prev = rec.this_hash
        if len([ln for ln in lines if ln.strip()]) > len(kept_lines):
            tmp = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._root, prefix=".audit.", suffix=".tmp", delete=False
            )
            try:
                with tmp as handle:
                    handle.writelines(kept_lines)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp.name, self._path)
            except OSError as error:
                raise DrillAuditError(f"修复演练审计失败: {error}", component="drill.audit") from error
        self._records = kept_records
        if os.path.isfile(self._seals_path):
            with open(self._seals_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        try:
                            self._sealed_sessions.add(str(json.loads(line)["seal_for"]))
                        except (KeyError, ValueError):
                            continue
