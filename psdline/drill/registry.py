"""会话登记处：演练会话的名册与状态机落盘。

名册是一个原子替换的 ``sessions.json``（读出来、改、临时文件 + ``os.replace``），
每个会话另有独立目录存放哈希链、分叉副本与封存报告。并发由两道锁保证：

- 进程内：一把全局可重入锁保护名册的读-改-写；
- 跨进程：每个会话一把 ``fcntl`` 文件锁（``sessions/<id>.lock``）。同一时刻
  只有一个执行体能处置某会话——值班员 A 在演，值班员 B 的进程/线程只能看到
  "会话被占用"，不会写出交错的事件链。

名册只记摘要；真相源是各会话目录里的哈希链与 ``meta.json``。名册损坏可以从
会话目录重建（:meth:`rebuild_index`），这对崩溃恢复很关键。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Iterator

from .errors import SessionNotFoundError, SessionStateError
from .model import (
    ABANDONED,
    DRILL_MODES,
    SEALED,
    TERMINAL_STATES,
    can_transition,
)

SESSIONS_INDEX = "sessions.json"
META_FILE = "meta.json"
LOCK_SUFFIX = ".lock"
FORK_DIR = "fork"
REPORT_FILE = "report.json"

try:
    import fcntl  # POSIX；本系统运行在 Linux

    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - 非 POSIX 降级
    _HAVE_FLOCK = False


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionLock:
    """会话级跨进程文件锁（非阻塞：拿不到说明别人正在演）。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._handle = None

    def acquire(self, *, blocking: bool = False) -> bool:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._handle = open(self._path, "a+", encoding="utf-8")
        flags = fcntl.LOCK_EX if _HAVE_FLOCK else 0
        if not blocking:
            flags |= fcntl.LOCK_NB if _HAVE_FLOCK else 0
        try:
            if _HAVE_FLOCK:
                fcntl.flock(self._handle.fileno(), flags)
        except (BlockingIOError, OSError):
            self._handle.close()
            self._handle = None
            return False
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if _HAVE_FLOCK:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class SessionRegistry:
    """演练会话名册。"""

    def __init__(self, drill_root: str) -> None:
        self._root = os.path.abspath(drill_root)
        os.makedirs(self._root, exist_ok=True)
        self._index_path = os.path.join(self._root, SESSIONS_INDEX)
        self._mem_lock = threading.RLock()

    @property
    def root(self) -> str:
        return self._root

    # ---- 路径 ---------------------------------------------------------

    def session_dir(self, session_id: str) -> str:
        safe = _safe_id(session_id)
        return os.path.join(self._root, "sessions", safe)

    def fork_dir(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), FORK_DIR)

    def lock_path(self, session_id: str) -> str:
        return self.session_dir(session_id) + LOCK_SUFFIX

    def meta_path(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), META_FILE)

    def report_path(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), REPORT_FILE)

    def session_lock(self, session_id: str) -> SessionLock:
        return SessionLock(self.lock_path(session_id))

    # ---- 名册 ---------------------------------------------------------

    def _read_index(self) -> dict[str, dict]:
        if not os.path.exists(self._index_path):
            return {}
        with open(self._index_path, "r", encoding="utf-8") as handle:
            try:
                raw = json.load(handle)
            except json.JSONDecodeError:
                return {}
        sessions = raw.get("sessions", {})
        return sessions if isinstance(sessions, dict) else {}

    def _write_index(self, sessions: dict[str, dict]) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"sessions": sessions}, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._index_path)

    def list_sessions(self) -> list[dict]:
        """返回名册中的全部会话摘要（按创建时间）。"""

        with self._mem_lock:
            sessions = list(self._read_index().values())
        sessions.sort(key=lambda item: item.get("created_at", ""))
        return sessions

    def get(self, session_id: str) -> dict:
        # 权威源是 meta.json（含完整字段）；名册只是检索摘要，绝不拿摘要冒充权威。
        meta = self.read_meta(session_id)
        if meta is not None:
            return meta
        with self._mem_lock:
            fallback = self._read_index().get(session_id)
        if fallback is None:
            raise SessionNotFoundError(f"演练会话不存在: {session_id}")
        return fallback

    def exists(self, session_id: str) -> bool:
        try:
            self.get(session_id)
            return True
        except SessionNotFoundError:
            return False

    # ---- 创建 ---------------------------------------------------------

    def create(
        self,
        *,
        mode: str,
        scenario: str,
        actor: str,
        snapshot_digest: str = "",
        title: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """登记一个新会话（PENDING），建目录、写权威 meta、更新名册。"""

        if mode not in DRILL_MODES:
            raise SessionStateError(f"未知演练模式: {mode}")
        session_id = f"DR-{uuid.uuid4().hex[:12]}"
        with self._mem_lock:
            directory = self.session_dir(session_id)
            os.makedirs(directory, exist_ok=False)
            now = utc_stamp()
            meta = {
                "id": session_id,
                "title": title or f"{scenario} 演练",
                "mode": mode,
                "scenario": scenario,
                "state": "pending",
                "actor": actor,
                "snapshot_digest": snapshot_digest,
                "created_at": now,
                "updated_at": now,
                "sim_time": 0.0,
                "tick_count": 0,
                "request_count": 0,
                "recovered": False,
                "recovery_note": "",
                "terminated_at": "",
                "termination_reason": "",
                "metadata": metadata or {},
            }
            self._write_meta(session_id, meta)
            sessions = self._read_index()
            sessions[session_id] = _index_summary(meta)
            self._write_index(sessions)
            return meta

    # ---- meta（权威） -------------------------------------------------

    def read_meta(self, session_id: str) -> dict | None:
        path = self.meta_path(session_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_meta(self, session_id: str, meta: dict) -> None:
        path = self.meta_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def update_meta(self, session_id: str, **changes: object) -> dict:
        """更新权威 meta 并同步名册摘要（调用方需持有会话锁）。"""

        with self._mem_lock:
            meta = self.read_meta(session_id)
            if meta is None:
                raise SessionNotFoundError(f"演练会话不存在: {session_id}")
            meta.update(changes)
            meta["updated_at"] = utc_stamp()
            self._write_meta(session_id, meta)
            sessions = self._read_index()
            if session_id in sessions:
                sessions[session_id] = _index_summary(meta)
                self._write_index(sessions)
            return meta

    def transition(self, session_id: str, target: str, **changes: object) -> dict:
        """按状态机白名单推进会话状态，非法跳转拒绝。"""

        with self._mem_lock:
            meta = self.read_meta(session_id)
            if meta is None:
                raise SessionNotFoundError(f"演练会话不存在: {session_id}")
            current = str(meta.get("state", ""))
            if not can_transition(current, target):
                raise SessionStateError(
                    f"会话 {session_id} 状态不允许从 {current} 转到 {target}",
                    component="drill-registry",
                )
            for key, value in changes.items():
                meta[key] = value
            meta["state"] = target
            meta["updated_at"] = utc_stamp()
            if target in TERMINAL_STATES:
                meta["terminated_at"] = meta["updated_at"]
            self._write_meta(session_id, meta)
            sessions = self._read_index()
            if session_id in sessions:
                sessions[session_id] = _index_summary(meta)
                self._write_index(sessions)
            return meta

    # ---- 恢复与重建 ---------------------------------------------------

    def scan_all(self) -> list[dict]:
        """扫描会话目录，读全部权威 meta（崩溃后名册不全时用它恢复）。"""

        base = os.path.join(self._root, "sessions")
        if not os.path.isdir(base):
            return []
        metas = []
        for name in sorted(os.listdir(base)):
            meta_path = os.path.join(base, name, META_FILE)
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as handle:
                    metas.append(json.load(handle))
            except (OSError, json.JSONDecodeError):
                continue
        return metas

    def rebuild_index(self) -> int:
        """从各会话目录的权威 meta 重建名册，返回会话数。"""

        with self._mem_lock:
            sessions: dict[str, dict] = {}
            for meta in self.scan_all():
                sessions[str(meta["id"])] = _index_summary(meta)
            self._write_index(sessions)
            return len(sessions)


@contextlib.contextmanager
def locked_session(registry: SessionRegistry, session_id: str, *, blocking: bool = False) -> Iterator[None]:
    """会话锁上下文：进入时拿跨进程锁，退出时释放。"""

    lock = registry.session_lock(session_id)
    if not lock.acquire(blocking=blocking):
        raise SessionStateError(
            f"会话 {session_id} 正被其他执行体占用，拒绝并发处置",
            component="drill-registry",
        )
    try:
        yield
    finally:
        lock.release()


def _safe_id(session_id: str) -> str:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not safe or safe != session_id:
        raise SessionStateError(f"非法会话标识: {session_id}", component="drill-registry")
    return safe


def _index_summary(meta: dict) -> dict:
    """名册只放摘要字段，控制单文件体积。"""

    keys = (
        "id", "title", "mode", "scenario", "state", "actor", "snapshot_digest",
        "created_at", "updated_at", "terminated_at", "recovered",
    )
    return {key: meta.get(key, "") for key in keys}
