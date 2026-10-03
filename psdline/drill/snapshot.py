"""故障时刻（或当前时刻）现场快照：只读捕获、内容寻址、一次写入。

演练的第一条安全边界是「事实从哪来」。快照在捕获时**只读**现场存储：把现场
``JsonStore`` 下的全部当前态 JSON 文档原样读出来，逐文档算 SHA-256，写进一个
内容寻址、永不修改的快照包。之后所有演练都从快照包复制出的独立沙箱启动，现场
目录在整个捕获过程中不被写入、不被加锁、不被改名。

快照目录布局（独立于现场存储根）::

    <drill_root>/snapshots/<snapshot_id>/
        manifest.json     # 清单（含逐文档哈希、捕获元数据、整体指纹）
        docs/<doc路径>.json

``snapshot_id`` 取文档集内容哈希，因此同一份现场状态重复捕获得到同一快照，
也意味着快照包一旦写成就不可被「另存覆盖」——内容不同就是另一个快照。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Any

from ..errors import RailError
from ..store.jsonfile import JsonStore
from .canonical import digest

SNAPSHOT_KIND_REPLAY = "fault_replay"
SNAPSHOT_KIND_LIVE = "live_fork"
SNAPSHOT_VERSION = 1


class SnapshotError(RailError):
    """快照捕获、校验或恢复失败。"""


@dataclass(frozen=True)
class DocRecord:
    """快照里的单个文档及其内容哈希。"""

    doc_id: str
    sha256: str
    bytes_len: int

    def as_dict(self) -> dict:
        return {"doc_id": self.doc_id, "sha256": self.sha256, "bytes_len": self.bytes_len}

    @classmethod
    def from_dict(cls, payload: dict) -> "DocRecord":
        return cls(
            doc_id=str(payload["doc_id"]),
            sha256=str(payload["sha256"]),
            bytes_len=int(payload.get("bytes_len", 0)),
        )


class SnapshotStore:
    """内容寻址、只写一次的快照仓库。"""

    def __init__(self, drill_root: str) -> None:
        self._root = os.path.abspath(drill_root)
        self._snap_dir = os.path.join(self._root, "snapshots")
        os.makedirs(self._snap_dir, exist_ok=True)

    @property
    def root(self) -> str:
        return self._root

    def _snapshot_path(self, snapshot_id: str) -> str:
        return os.path.join(self._snap_dir, snapshot_id)

    def exists(self, snapshot_id: str) -> bool:
        return os.path.isfile(os.path.join(self._snapshot_path(snapshot_id), "manifest.json"))

    def list_ids(self) -> list[str]:
        if not os.path.isdir(self._snap_dir):
            return []
        found = []
        for name in os.listdir(self._snap_dir):
            if self.exists(name):
                found.append(name)
        return sorted(found)

    def capture(
        self,
        live_store: JsonStore,
        *,
        kind: str,
        label: str,
        captured_at: float,
        source_fingerprint: str = "",
        note: str = "",
    ) -> str:
        """从现场存储只读捕获一份快照，返回快照编号。

        全程不向 ``live_store`` 写任何东西。先把文档读进内存并算好内容指纹，
        再决定目标目录；若该内容快照已存在则直接复用（幂等、不覆盖）。
        """

        if kind not in (SNAPSHOT_KIND_REPLAY, SNAPSHOT_KIND_LIVE):
            raise SnapshotError(f"未知快照类型: {kind}", component="drill.snapshot")

        doc_ids = live_store.list_ids()
        records: list[DocRecord] = []
        payloads: dict[str, Any] = {}
        for doc_id in doc_ids:
            path = live_store.path_of(doc_id)
            try:
                with open(path, "rb") as handle:
                    raw = handle.read()
            except OSError as error:
                raise SnapshotError(
                    f"只读捕获现场文档失败 {doc_id}: {error}", component="drill.snapshot"
                ) from error
            # 以「规范化后的 JSON 值」算哈希，使指纹描述的是数据而非磁盘排版；
            # 同时保留原字节长度做交叉核对。
            value = json.loads(raw.decode("utf-8"))
            payloads[doc_id] = value
            records.append(
                DocRecord(doc_id=doc_id, sha256=digest(value), bytes_len=len(raw))
            )
        records.sort(key=lambda item: item.doc_id)

        body = {
            "docs": [item.as_dict() for item in records],
            "source_root": live_store.root,
            "kind": kind,
            "label": label,
            "note": note,
            "captured_at": captured_at,
            "source_fingerprint": source_fingerprint,
            "version": SNAPSHOT_VERSION,
        }
        # 快照身份只由「文档内容 + 事实来源」决定，不含时间戳，保证幂等可复算。
        identity = {
            "docs": [item.as_dict() for item in records],
            "source_root": live_store.root,
        }
        snapshot_id = "snap-" + digest(identity)[:16]
        target = self._snapshot_path(snapshot_id)

        if self.exists(snapshot_id):
            # 已存在：核对内容一致后复用，绝不重写。
            self.verify(snapshot_id)
            return snapshot_id

        docs_dir = os.path.join(target, "docs")
        tmp_target = target + ".capturing"
        if os.path.exists(tmp_target):
            shutil.rmtree(tmp_target, ignore_errors=True)
        os.makedirs(os.path.join(tmp_target, "docs"), exist_ok=True)
        try:
            for doc_id in sorted(payloads):
                self._write_doc(os.path.join(tmp_target, "docs"), doc_id, payloads[doc_id])
            manifest = dict(body)
            manifest["snapshot_id"] = snapshot_id
            manifest["fingerprint"] = digest(body)
            self._atomic_write_json(os.path.join(tmp_target, "manifest.json"), manifest)
            os.replace(tmp_target, target)
        finally:
            shutil.rmtree(tmp_target, ignore_errors=True)
        return snapshot_id

    def manifest(self, snapshot_id: str) -> dict:
        path = os.path.join(self._require(snapshot_id), "manifest.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise SnapshotError(f"读取快照清单失败: {error}", component="drill.snapshot") from error

    def restore_into(self, snapshot_id: str, target_store: JsonStore) -> dict:
        """把快照内容复制进一个**应当为空**的演练沙箱存储。

        只允许恢复到演练根下的沙箱目录（路径必须与快照来源根不同），从机制上
        杜绝把快照写回现场目录。
        """

        base = self._require(snapshot_id)
        manifest = self.manifest(snapshot_id)
        if os.path.abspath(target_store.root) == os.path.abspath(manifest["source_root"]):
            raise SnapshotError("拒绝把快照恢复进现场存储根", component="drill.snapshot")
        sessions_root = os.path.join(self._root, "sessions")
        if os.path.commonpath([os.path.abspath(target_store.root), sessions_root]) != sessions_root:
            raise SnapshotError("沙箱必须位于演练根的 sessions/ 子树下", component="drill.snapshot")

        restored = 0
        for record in manifest["docs"]:
            rec = DocRecord.from_dict(record)
            value = self._read_doc(os.path.join(base, "docs"), rec.doc_id)
            if digest(value) != rec.sha256:
                raise SnapshotError(
                    f"快照文档哈希不一致: {rec.doc_id}", component="drill.snapshot"
                )
            target_store.write(rec.doc_id, value)
            restored += 1
        return {"restored": restored, "snapshot_id": snapshot_id}

    def verify(self, snapshot_id: str) -> dict:
        """核对清单整体指纹与逐文档哈希，返回校验结果。"""

        base = self._require(snapshot_id)
        manifest = self.manifest(snapshot_id)
        stored_fp = manifest.get("fingerprint", "")
        body = {key: value for key, value in manifest.items() if key not in ("snapshot_id", "fingerprint")}
        results = {"snapshot_id": snapshot_id, "ok": True, "docs": 0, "mismatches": []}
        if digest(body) != stored_fp:
            results["ok"] = False
            results["mismatches"].append("manifest")
        for record in manifest.get("docs", []):
            rec = DocRecord.from_dict(record)
            value = self._read_doc(os.path.join(base, "docs"), rec.doc_id)
            results["docs"] += 1
            if digest(value) != rec.sha256:
                results["ok"] = False
                results["mismatches"].append(rec.doc_id)
        if results["mismatches"]:
            raise SnapshotError(
                f"快照完整性校验失败: {results['mismatches']}", component="drill.snapshot"
            )
        return results

    # ---- 内部 ---------------------------------------------------------

    def _require(self, snapshot_id: str) -> str:
        self._guard_id(snapshot_id)
        path = self._snapshot_path(snapshot_id)
        if not os.path.isfile(os.path.join(path, "manifest.json")):
            raise SnapshotError(f"快照不存在: {snapshot_id}", component="drill.snapshot")
        return path

    @staticmethod
    def _guard_id(snapshot_id: str) -> None:
        if not snapshot_id.startswith("snap-") or "/" in snapshot_id or ".." in snapshot_id:
            raise SnapshotError(f"非法快照编号: {snapshot_id}", component="drill.snapshot")

    @staticmethod
    def _doc_relpath(docs_root: str, doc_id: str) -> str:
        relative = doc_id.replace("\\", "/").strip("/")
        if not relative or ".." in relative.split("/"):
            raise SnapshotError(f"非法文档标识: {doc_id}", component="drill.snapshot")
        return os.path.join(docs_root, *relative.split("/")) + ".json"

    def _write_doc(self, docs_root: str, doc_id: str, value: Any) -> None:
        target = self._doc_relpath(docs_root, doc_id)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        self._atomic_write_json(target, value)

    def _read_doc(self, docs_root: str, doc_id: str) -> Any:
        target = self._doc_relpath(docs_root, doc_id)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise SnapshotError(f"读取快照文档失败 {doc_id}: {error}", component="drill.snapshot") from error

    @staticmethod
    def _atomic_write_json(target: str, value: Any) -> None:
        import tempfile

        directory = os.path.dirname(target)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".snap.", suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, target)
        except OSError as error:
            raise SnapshotError(f"写快照失败: {error}", component="drill.snapshot") from error
