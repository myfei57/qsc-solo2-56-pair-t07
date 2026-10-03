"""快照与分叉：现场 → 演练的唯一数据通道。

安全边界的关键一环：演练世界**只能**通过这里获得现场数据，且是**一次性复制**
（copy-on-fork），不是软链接、不是共享目录。之后演练平台在自己的副本上推演，
对副本做的任何写操作都不可能回到现场存储根。

快照包内容（``snapshots/<hash12>/bundle.json``）：
  - ``captured_at``  墙上时钟（什么时候拍的，注释用）；
  - ``source``       ``live``（实时分叉）或 ``replay:<session>``（从旧快照重放）；
  - ``documents``    现场存储根下全部 JSON 文档的 {doc_id: content}；
  - ``audit_tail``   现场审计 JSONL 尾部（只读观察用，不进入分叉）。

内容指纹 = sha256(规范化(documents 排序后))。同一份现场状态无论拍多少次、在哪个
进程拍，指纹一致，所以快照天然去重且不可改：改一个字节就换一个目录。

重放模式拍快照时会同时固化"故障时刻"：剧本与注入都以此刻为零点。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass

from ..clock import Clock
from ..store import JsonStore
from .crypto import content_hash

SNAPSHOT_DIR = "snapshots"
BUNDLE_FILE = "bundle.json"
AUDIT_TAIL_LIMIT = 200


@dataclass(frozen=True)
class SnapshotBundle:
    """一次现场快照的描述。"""

    digest: str
    path: str
    captured_at: str
    source: str
    document_count: int

    @property
    def short(self) -> str:
        return self.digest[:12]

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "short": self.short,
            "path": self.path,
            "captured_at": self.captured_at,
            "source": self.source,
            "document_count": self.document_count,
        }


class SnapshotStore:
    """演练侧的内容寻址快照仓。"""

    def __init__(self, drill_root: str, clock: Clock) -> None:
        self._root = os.path.abspath(drill_root)
        self._dir = os.path.join(self._root, SNAPSHOT_DIR)
        os.makedirs(self._dir, exist_ok=True)
        self._clock = clock

    def _bundle_path(self, digest: str) -> str:
        return os.path.join(self._dir, digest[:12], BUNDLE_FILE)

    def capture_from_live(
        self,
        live_store: JsonStore,
        *,
        source: str = "live",
        audit_tail_limit: int = AUDIT_TAIL_LIMIT,
    ) -> SnapshotBundle:
        """从现场存储根复制全部当前态文档，落为不可变快照。

        ``source`` 标识来源（``live`` 或 ``replay:<id>``）。复制走"读全部文档
        → 规范化落 bundle"，不复制文件本身，避免把现场文件权限/临时文件带过来。
        """

        documents: dict[str, object] = {}
        for doc_id in live_store.list_ids():
            documents[doc_id] = live_store.read(doc_id)
        digest = content_hash({"documents": documents})
        return self._write_bundle(digest, documents, source, audit_tail=None,
                                 live_store=live_store, limit=audit_tail_limit)

    def _write_bundle(
        self,
        digest: str,
        documents: dict[str, object],
        source: str,
        *,
        audit_tail: list[dict] | None,
        live_store: JsonStore | None = None,
        limit: int = AUDIT_TAIL_LIMIT,
    ) -> SnapshotBundle:
        path = self._bundle_path(digest)
        captured = self._clock.utc_iso()
        if os.path.exists(path):
            # 同内容快照已存在：内容寻址保证等价，直接复用，不覆盖。
            return SnapshotBundle(digest, path, captured, source, len(documents))
        if audit_tail is None and live_store is not None:
            audit_tail = _read_audit_tail(live_store.root, limit)
        bundle = {
            "digest": digest,
            "captured_at": captured,
            "source": source,
            "documents": documents,
            "audit_tail": audit_tail or [],
        }
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(bundle, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o444)  # 快照包一经落盘只读
        os.chmod(directory, 0o555)
        return SnapshotBundle(digest, path, captured, source, len(documents))

    def load(self, digest: str) -> dict:
        """读取一个快照包，不存在返回空 dict（调用方判定）。"""

        path = self._bundle_path(digest)
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def list(self) -> list[dict]:
        """列出全部快照（按指纹排序）。"""

        found = []
        if not os.path.isdir(self._dir):
            return found
        for name in sorted(os.listdir(self._dir)):
            path = os.path.join(self._dir, name, BUNDLE_FILE)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as handle:
                    bundle = json.load(handle)
                found.append(
                    {
                        "digest": bundle["digest"],
                        "short": bundle["digest"][:12],
                        "captured_at": bundle.get("captured_at", ""),
                        "source": bundle.get("source", ""),
                        "document_count": len(bundle.get("documents", {})),
                    }
                )
        return found

    def fork_to(self, digest: str, fork_root: str) -> dict:
        """把快照还原成一个可写的现场副本（演练平台的存储根）。

        只还原"当前态文档"；现场审计流水不复制（演练有自己的哈希链日志，
        两条审计永远不共用文件）。
        """

        bundle = self.load(digest)
        if not bundle:
            raise FileNotFoundError(f"快照不存在: {digest[:12]}")
        if os.path.abspath(fork_root) == self._dir or os.path.abspath(fork_root).startswith(
            self._dir + os.sep
        ):
            raise ValueError("分叉目录不能落在快照仓内")
        os.makedirs(fork_root, exist_ok=True)
        store = JsonStore(fork_root)
        for doc_id, content in bundle["documents"].items():
            store.write(doc_id, content)
        return {
            "fork_root": os.path.abspath(fork_root),
            "documents": len(bundle["documents"]),
            "snapshot": digest,
        }


def _read_audit_tail(live_root: str, limit: int) -> list[dict]:
    """读现场审计 JSONL 的尾部，作为快照里的只读观察（不带进分叉）。"""

    records: list[dict] = []
    if not os.path.isdir(live_root):
        return records
    names = sorted(
        name for name in os.listdir(live_root)
        if name.startswith("audit-") and name.endswith(".jsonl")
    )
    for name in reversed(names):
        path = os.path.join(live_root, name)
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        chunk: list[dict] = []
        for line in reversed(lines):
            try:
                chunk.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(records) + len(chunk) >= limit:
                break
        records.extend(reversed(chunk))
        if len(records) >= limit:
            break
    records.reverse()
    return records[-limit:]


#: 世界指纹中排除的身份/注释性文档：它们随运行实例名变化，不反映业务世界。
FINGERPRINT_EXCLUDE = frozenset({"platform/state"})


def world_fingerprint(fork_store: JsonStore) -> str:
    """对分叉存储根里的业务文档算内容指纹（状态检查点用）。

    现场审计 JSONL 不参与（演练存储根里本就没有它）；``platform/state`` 这类
    只记录运行实例名（DRILL/<会话> / VERIFY/<会话>）的身份文档也排除，保证
    "同业务状态"在不同执行体里指纹一致，可被重放比对。
    """

    documents = {
        doc_id: fork_store.read(doc_id)
        for doc_id in fork_store.list_ids()
        if doc_id not in FINGERPRINT_EXCLUDE
    }
    return content_hash({"documents": documents})
