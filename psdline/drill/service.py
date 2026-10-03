"""演练会话服务：编排门控、沙箱、事件流水、检查点、审计与复盘。

它把“演练、门控、审计、存储”四件事放到同一个提交协议里，保证演练痕迹绝不混进
现场事实，并覆盖并发演练、部分失败恢复、崩溃安全与历史不可变。

关键事实
========
* **存储物理分离**：现场在 ``storage_root``；演练在独立 ``drill_root``，下设
  ``snapshots/``（不可变快照）与 ``sessions/<id>/``（每会话一套：沙箱、事件流、
  检查点、元数据、锁）。两个根目录不重叠。
* **门控 fail-closed**：每步动作先过门（演练模式永不 ``transmitted``），门拒绝
  则一步沙箱逻辑都不执行。
* **事件先行（write-ahead）**：动作在沙箱执行成功后，先把结果作为
  ``operator_action`` 哈希链事件 fsync 落盘，再更新检查点。崩溃在两者之间也
  安全——重放事件流即可重建；未及落事件的动作等于没发生，可凭
  ``client_request_id`` 安全重试（演练无现场副作用，重放幂等）。
* **状态由历史决定**：打开会话时用事件流重放校验检查点；不一致就丢弃检查点
  全量重建。现场状态永远可从“快照 + 事件流”复算。
* **并发会话互不影响**：每会话独立目录 + 独立 fcntl 锁 + 独立令牌。
* **不可逆切换**：会话内不允许“切回实作”，会话是纯沙箱；真作通道始终是另一套
  独立现场平台，演练全程不受影响也不被触碰。``abort`` 一键结束演练。
"""

from __future__ import annotations

import json
import os
import shutil
import time

from ..config import PlatformConfig, default_config
from ..errors import RailError
from ..store.jsonfile import JsonStore
from . import actions as action_mod
from .audit import DrillAuditError, DrillAuditLog
from .basis import build_basis
from .canonical import digest, short
from .checkpoint import Checkpoint, CheckpointStore
from .eventlog import ChainedEventLog
from .flock import file_lock
from .gate import (
    DRILL,
    CommandEnvelope,
    CommandGate,
    DrillTokenSigner,
    FieldGateway,
)
from .replay import replay_events
from .sandbox import build_sandbox, fingerprint_store
from .scenario import get_scenario
from .simulator import Simulator
from .snapshot import SNAPSHOT_KIND_LIVE, SNAPSHOT_KIND_REPLAY, SnapshotStore

STATUS_OPEN = "open"
STATUS_ABORTED = "aborted"
STATUS_ENDED = "ended"


def _resolve_secret(secret: bytes | None, drill_root: str) -> bytes:
    """确定演练 HMAC 密钥：显式 > 环境变量 > 演练根持久密钥文件。

    CLI 每条命令都是新进程，封存 HMAC 也必须在重启后仍能复核，因此不能用进程内
    一次性随机密钥。首次使用时在演练根生成一个仅属主可读写的密钥文件。
    """

    if secret is not None:
        return secret
    env = os.environ.get("PSDLINE_DRILL_HMAC", "")
    if env:
        return env.encode("utf-8")
    key_path = os.path.join(os.path.abspath(drill_root), "drill.key")
    if os.path.isfile(key_path):
        with open(key_path, "rb") as handle:
            data = handle.read().strip()
        if len(data) >= 16:
            return data
    generated = os.urandom(32).hex().encode("ascii")
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, generated)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return generated


class DrillError(RailError):
    """演练会话生命周期错误。"""


class DrillService:
    """整个演练子系统的入口（一个现场存储根对应一个服务实例）。"""

    def __init__(
        self,
        drill_root: str,
        live_store: JsonStore,
        config: PlatformConfig | None = None,
        *,
        secret: bytes | None = None,
    ) -> None:
        self._drill_root = os.path.abspath(drill_root)
        self._live_store = live_store
        self._config = (config or default_config()).validate()
        self._sessions_root = os.path.join(self._drill_root, "sessions")
        os.makedirs(self._sessions_root, exist_ok=True)
        self._snapshots = SnapshotStore(self._drill_root)
        # 密钥必须跨进程稳定（CLI 每条命令都是新进程，且封存 HMAC 要能在重启后
        # 复核）：显式注入 > 环境变量 > 演练根下 0600 持久密钥文件。
        self._signer = DrillTokenSigner(_resolve_secret(secret, self._drill_root))
        self._audit = DrillAuditLog(self._drill_root, self._signer._secret)  # noqa: SLF001
        # 现场出口实例被演练服务引用，但演练门在 DRILL 模式从不调用它。
        self._field = FieldGateway()
        # 进程内会话上下文缓存：session_id -> {"head", "platform", "sim", "meta"}。
        # 同一进程连续多步操作复用工作沙箱；仅当事件流链顶变化（典型为另一个进程
        # 动过同一会话）时才丢弃缓存、用不可变事件流重放恢复。
        self._context_cache: dict[str, dict] = {}

    @property
    def audit(self) -> DrillAuditLog:
        return self._audit

    @property
    def snapshots(self) -> SnapshotStore:
        return self._snapshots

    @property
    def field_gateway(self) -> FieldGateway:
        return self._field

    # ---- 快照 ---------------------------------------------------------

    def capture_snapshot(self, kind: str, label: str, note: str = "") -> str:
        before = fingerprint_store(self._live_store)
        captured_at = time.time()
        snapshot_id = self._snapshots.capture(
            self._live_store,
            kind=kind,
            label=label,
            captured_at=captured_at,
            source_fingerprint=before["fingerprint"],
            note=note,
        )
        after = fingerprint_store(self._live_store)
        if after["fingerprint"] != before["fingerprint"]:
            # 捕获只读、不应改动现场；若变了说明捕获过程中现场在演进（正常），
            # 记录差异但快照本身仍自洽。
            self._audit.record(
                "snapshot_live_changed_during_capture",
                snapshot_id,
                before=before["fingerprint"],
                after=after["fingerprint"],
            )
        self._audit.record(
            "snapshot_captured",
            snapshot_id,
            snapshot_kind=kind,
            label=label,
            source_fingerprint=before["fingerprint"],
            docs=before["doc_count"],
        )
        return snapshot_id

    # ---- 会话生命周期 -------------------------------------------------

    def start_session(
        self,
        scenario_key: str,
        *,
        snapshot_id: str,
        operator: str,
        mode: str = SNAPSHOT_KIND_REPLAY,
        reason: str = "",
    ) -> dict:
        scenario = get_scenario(scenario_key)
        self._snapshots.verify(snapshot_id)
        seed = {
            "snapshot_id": snapshot_id,
            "scenario": scenario_key,
            "operator": operator,
            "nonce": time.time_ns(),
        }
        session_id = "drill-" + short(seed)
        session_dir = self._session_dir(session_id)
        if os.path.exists(session_dir):
            raise DrillError(f"演练会话目录冲突: {session_id}", component="drill")
        os.makedirs(session_dir, exist_ok=False)
        sandbox_root = os.path.join(session_dir, "sandbox")
        os.makedirs(sandbox_root, exist_ok=False)

        # 复制快照到全新沙箱（restore_into 会校验目标在 sessions/ 下且非现场根）。
        sandbox_store = JsonStore(sandbox_root)
        restored = self._snapshots.restore_into(snapshot_id, sandbox_store)

        platform = build_sandbox(self._config, sandbox_root, scenario.start_at)
        token = self._signer.issue(session_id, snapshot_id)
        metadata = {
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "scenario": scenario_key,
            "operator": operator,
            "mode": mode,
            "reason": reason,
            "status": STATUS_OPEN,
            "created_at": time.time(),
            "start_at": scenario.start_at,
            "restored_docs": restored["restored"],
            "token": token,
        }
        self._write_metadata(session_dir, metadata)

        eventlog = ChainedEventLog(session_dir)
        eventlog.append(
            "session_begin",
            metadata["created_at"],
            0,
            {
                "snapshot_id": snapshot_id,
                "scenario": scenario_key,
                "operator": operator,
                "mode": mode,
                "reason": reason,
                "start_at": scenario.start_at,
                "restored_docs": restored["restored"],
                "source_fingerprint": self._snapshots.manifest(snapshot_id).get("fingerprint"),
            },
        )
        # 初始检查点：空动作序列，指纹即沙箱起点。
        fp = fingerprint_store(platform.store)
        CheckpointStore(session_dir).save(
            Checkpoint(
                event_count=1,
                tick=0,
                clock_at=platform.clock.now(),
                state_fingerprint=fp["fingerprint"],
            )
        )
        self._audit.record(
            "session_begin",
            session_id,
            snapshot_id=snapshot_id,
            scenario=scenario_key,
            operator=operator,
            mode=mode,
        )
        return {"session_id": session_id, **{k: v for k, v in metadata.items() if k != "token"}, "token": token}

    def act(
        self,
        session_id: str,
        action: str,
        args: dict,
        client_request_id: str,
        token: str,
    ) -> dict:
        """值班员执行一步处置（门控 → 判据 → 沙箱执行 → WAL → 检查点）。"""

        with self._locked(session_id):
            ctx = self._open_context(session_id)
            metadata, eventlog, ckpt_store = ctx["meta"], ctx["events"], ctx["ckpt"]
            platform, sim = ctx["platform"], ctx["sim"]

            if metadata.get("status") != STATUS_OPEN:
                raise DrillError(
                    f"演练会话已{('中止' if metadata.get('status') == STATUS_ABORTED else '结束封存')}，不可再操作: {session_id}",
                    component="drill",
                )

            # 幂等：同一 client_request_id 重放，直接返回首次结果，不重复执行。
            prior = self._find_prior(eventlog, client_request_id)
            if prior is not None:
                self._audit.record(
                    "operator_action_idempotent_replay",
                    session_id,
                    client_request_id=client_request_id,
                    action=action,
                )
                return {"idempotent": True, "seq": prior.seq, **_action_view(prior)}

            gate = self._gate(metadata, eventlog)
            envelope = CommandEnvelope.of(
                {
                    "action": action,
                    "mode": DRILL,
                    "session_id": session_id,
                    "token": token,
                    "args": args,
                    "client_request_id": client_request_id,
                    "issued_at": time.time(),
                }
            )
            verdict = gate.screen(envelope)
            basis = build_basis(platform, action, args) if action in action_mod.ACTION_SPECS else None

            if not verdict.allowed:
                # 门拒绝：不执行任何沙箱动作，仅落一条被拒事件，便于复盘越权尝试。
                event = eventlog.append(
                    "operator_action",
                    time.time(),
                    sim.tick,
                    {
                        "client_request_id": client_request_id,
                        "action": action,
                        "args": dict(args),
                        "gate": verdict.as_dict(),
                        "basis": basis,
                        "executed": False,
                        "result": None,
                    },
                )
                self._audit.record(
                    "operator_action_gated",
                    session_id,
                    client_request_id=client_request_id,
                    action=action,
                    reason=verdict.reason,
                )
                self._refresh_checkpoint(ckpt_store, eventlog, platform, sim)
                self._remember_context(ctx)
                return {"seq": event.seq, "ok": False, "gated": True, "verdict": verdict.as_dict(), "basis": basis}

            # 门放行（演练模式 transmitted=False）：在沙箱真实执行。
            try:
                result = action_mod.execute(platform, action, dict(args))
            except RailError as error:
                # 平台真实逻辑抛错（如未对位先确认间隙）：记录为一次“现场会拒绝”的处置。
                result = {"ok": False, "violations": [error.message], "report": None, "raised": error.as_record()}

            event = eventlog.append(
                "operator_action",
                time.time(),
                sim.tick,
                {
                    "client_request_id": client_request_id,
                    "action": action,
                    "args": dict(args),
                    "gate": verdict.as_dict(),
                    "basis": basis,
                    "executed": True,
                    "result": result,
                    "post_fingerprint": fingerprint_store(platform.store)["fingerprint"],
                },
            )
            self._refresh_checkpoint(ckpt_store, eventlog, platform, sim)
            self._audit.record(
                "operator_action",
                session_id,
                client_request_id=client_request_id,
                action=action,
                ok=result["ok"],
                violations=result["violations"],
                seq=event.seq,
            )
            self._remember_context(ctx)
            return {"seq": event.seq, "ok": result["ok"], "violations": result["violations"], "result": result, "basis": basis, "verdict": verdict.as_dict()}

    def advance(self, session_id: str, to_tick: int) -> dict:
        """推进演练时钟并注入到期的场景事件。"""

        with self._locked(session_id):
            ctx = self._open_context(session_id)
            metadata, eventlog, ckpt_store = ctx["meta"], ctx["events"], ctx["ckpt"]
            platform, sim = ctx["platform"], ctx["sim"]
            if metadata.get("status") != STATUS_OPEN:
                raise DrillError("演练已结束封存，不可再推进时钟", component="drill")
            if to_tick < sim.tick:
                raise DrillError("演练时钟不可回退", component="drill")
            effects = sim.advance(to_tick)
            event = eventlog.append(
                "sim_tick",
                time.time(),
                sim.tick,
                {"to_tick": sim.tick, "effects": effects,
                 "post_fingerprint": fingerprint_store(platform.store)["fingerprint"]},
            )
            self._refresh_checkpoint(ckpt_store, eventlog, platform, sim)
            self._audit.record("sim_tick", session_id, to_tick=sim.tick, injected=sum(len(e["injected"]) for e in effects))
            self._remember_context(ctx)
            return {"seq": event.seq, "tick": sim.tick, "effects": effects}

    def note(self, session_id: str, text: str, channel: str = "radio") -> dict:
        """登记一条不改变现场状态的过程信息（广播/联控/口头汇报）。"""

        with self._locked(session_id):
            ctx = self._open_context(session_id)
            event = ctx["events"].append(
                "note", time.time(), ctx["sim"].tick, {"text": text, "channel": channel}
            )
            self._audit.record("drill_note", session_id, channel=channel)
            self._remember_context(ctx)
            return {"seq": event.seq}

    def finish(self, session_id: str, aborted: bool = False, reason: str = "") -> dict:
        """结束演练（正常结束或一键中止）。结束后封存，历史不可再追加。"""

        with self._locked(session_id):
            ctx = self._open_context(session_id)
            metadata, eventlog = ctx["meta"], ctx["events"]
            if metadata["status"] != STATUS_OPEN:
                raise DrillError(f"会话已结束: {session_id}", component="drill")
            kind = "session_abort" if aborted else "session_end"
            chain = eventlog.verify_chain()
            event = eventlog.append(
                kind,
                time.time(),
                ctx["sim"].tick,
                {"reason": reason, "chain_head": chain["head_hash"], "events": chain["count"]},
            )
            status = STATUS_ABORTED if aborted else STATUS_ENDED
            metadata["status"] = status
            metadata["ended_at"] = time.time()
            metadata["end_reason"] = reason
            self._write_metadata(self._session_dir(session_id), metadata)
            # 状态已变为封存：丢弃进程内缓存，后续打开会读到已封存 metadata。
            self._context_cache.pop(session_id, None)
            report = self._build_report_locked(ctx)
            marker = self._audit.seal_session(session_id, {"events": chain["count"], "status": status, "score": report["score"]})
            return {"session_id": session_id, "status": status, "seq": event.seq, "seal": marker, "report": report}

    # ---- 查询与复盘 ---------------------------------------------------

    def status(self, session_id: str) -> dict:
        with self._locked(session_id):
            ctx = self._open_context(session_id)
            platform = ctx["platform"]
            return {
                "session_id": session_id,
                "status": ctx["meta"]["status"],
                "snapshot_id": ctx["meta"]["snapshot_id"],
                "scenario": ctx["meta"]["scenario"],
                "operator": ctx["meta"]["operator"],
                "tick": ctx["sim"].tick,
                "clock_at": platform.clock.now(),
                "events": len(ctx["events"].events),
                "sealed": self._audit.is_sealed(session_id),
                "chain": ctx["events"].verify_chain(),
                "field_transmitted": self._field.transmitted_count,
                "live": {
                    "open_gates": platform.gates.count_open(),
                    "locked": platform.faults.active_ids(),
                    "audit_entries_in_sandbox": len(platform.audit.list()),
                },
            }

    def build_report(self, session_id: str) -> dict:
        """生成逐步复盘（加锁外壳）。"""

        with self._locked(session_id):
            return self._build_report_locked(self._open_context(session_id))

    def _build_report_locked(self, ctx: dict) -> dict:
        """生成逐步复盘：每步动作、门控、判断依据、结果、与预判一致性，以及评分。"""

        from .report import score_scenario, step_records

        session_id = ctx["meta"]["session_id"]
        scenario = get_scenario(ctx["meta"]["scenario"])
        events = ctx["events"].events
        steps = step_records(events)
        scored = score_scenario(scenario, events)
        chain = ctx["events"].verify_chain()
        # 字节级复现：另起沙箱重放全部事件，指纹必须与工作沙箱一致。
        verify_root = os.path.join(self._session_dir(session_id), "verify-tmp")
        replay = replay_events(
            self._config,
            self._snapshots,
            ctx["meta"]["snapshot_id"],
            verify_root,
            [e for e in events if e.kind in ("operator_action", "sim_tick")],
            scenario,
            tick_seconds=scenario.tick_seconds,
        )
        shutil.rmtree(verify_root, ignore_errors=True)
        live_fp = fingerprint_store(ctx["platform"].store)["fingerprint"]
        return {
            "session_id": session_id,
            "scenario": {"key": scenario.key, "title": scenario.title, "brief": scenario.brief},
            "operator": ctx["meta"]["operator"],
            "mode": ctx["meta"]["mode"],
            "status": ctx["meta"]["status"],
            "snapshot_id": ctx["meta"]["snapshot_id"],
            "steps": steps,
            "checklist": scored["items"],
            "score": scored["score"],
            "max_score": scored["max_score"],
            "passed": scored["passed"],
            "violations_total": scored["violations_total"],
            "blocked_attempts": scored.get("blocked_attempts", 0),
            "field_transmitted": self._field.transmitted_count,
            "integrity": {
                "event_chain": chain,
                "replay_match": replay["fingerprint"] == live_fp,
                "replay_fingerprint": replay["fingerprint"],
                "live_fingerprint": live_fp,
            },
        }

    def list_sessions(self) -> list[dict]:
        found = []
        if not os.path.isdir(self._sessions_root):
            return found
        for name in sorted(os.listdir(self._sessions_root)):
            meta_path = os.path.join(self._sessions_root, name, "metadata.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as handle:
                        meta = json.load(handle)
                    found.append({k: meta.get(k) for k in ("session_id", "scenario", "operator", "status", "snapshot_id", "created_at")})
                except (OSError, json.JSONDecodeError):
                    continue
        return found

    # ---- 内部：会话打开与提交 ----------------------------------------

    def _session_dir(self, session_id: str) -> str:
        if not session_id.startswith("drill-") or "/" in session_id or ".." in session_id:
            raise DrillError(f"非法会话编号: {session_id}", component="drill")
        return os.path.join(self._sessions_root, session_id)

    def _locked(self, session_id: str):
        return file_lock(os.path.join(self._session_dir(session_id), "session.lock"))

    def _read_metadata(self, session_id: str) -> dict:
        path = os.path.join(self._session_dir(session_id), "metadata.json")
        if not os.path.isfile(path):
            raise DrillError(f"会话不存在: {session_id}", component="drill")
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_metadata(self, session_dir: str, metadata: dict) -> None:
        target = os.path.join(session_dir, "metadata.json")
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)

    def _gate(self, metadata: dict, eventlog: ChainedEventLog) -> CommandGate:
        session_id = metadata["session_id"]
        snapshot_id = metadata["snapshot_id"]
        open_sessions = {session_id} if metadata["status"] == STATUS_OPEN else set()
        return CommandGate(
            DRILL,
            self._signer,
            is_session_open=lambda sid: sid in open_sessions,
            snapshot_of=lambda sid: snapshot_id if sid == session_id else "",
            audit=lambda kind, detail: None,  # 门内细粒度审计由服务统一落盘
        )

    def _open_context(self, session_id: str) -> dict:
        """打开会话：优先复用进程内沙箱，链顶变化才用事件流重放恢复。

        正确性来自不可变事件流：工作沙箱只是事件流的缓存视图。每次打开都先核对
        事件流链顶与缓存是否一致；不一致（本进程刚追加会同步更新，真正会失配的是
        另一进程推进了会话）就丢弃缓存，按「快照 + 全部变更事件」确定性重放重建。
        崩溃后新进程无缓存，首次打开自然走一次重放自愈。
        """

        session_dir = self._session_dir(session_id)
        metadata = self._read_metadata(session_id)
        scenario = get_scenario(metadata["scenario"])
        eventlog = ChainedEventLog(session_dir)
        ckpt_store = CheckpointStore(session_dir)

        if eventlog.truncated_tail:
            self._audit.record("session_tail_truncated", session_id, dropped=eventlog.truncated_tail)

        cached = self._context_cache.get(session_id)
        if (
            cached is not None
            and cached["meta"].get("status") == metadata.get("status")
            and cached["head"] == eventlog.head_hash
            and cached["length"] == len(eventlog.events)
        ):
            platform = cached["platform"]
            sim = cached["sim"]
        else:
            platform, sim = self._rebuild_sandbox(session_dir, metadata, scenario, eventlog)
            self._context_cache[session_id] = {
                "head": eventlog.head_hash,
                "length": len(eventlog.events),
                "platform": platform,
                "sim": sim,
                "meta": metadata,
            }

        ctx = {
            "meta": metadata,
            "events": eventlog,
            "ckpt": ckpt_store,
            "platform": platform,
            "sim": sim,
        }
        return ctx

    def _rebuild_sandbox(self, session_dir: str, metadata: dict, scenario, eventlog: ChainedEventLog):
        """用快照 + 全部变更事件在工作沙箱目录确定性重建现场状态。"""

        sandbox_root = os.path.join(session_dir, "sandbox")
        mutating = [e for e in eventlog.events if e.kind in ("operator_action", "sim_tick")]

        # 先在临时目录重放得到期望终态，再与现有工作沙箱比对，决定就地扶正还是复用。
        verify_root = os.path.join(session_dir, "recover-verify")
        try:
            replay = replay_events(
                self._config,
                self._snapshots,
                metadata["snapshot_id"],
                verify_root,
                mutating,
                scenario,
                tick_seconds=scenario.tick_seconds,
            )
            expected_fp = replay["fingerprint"]
        finally:
            shutil.rmtree(verify_root, ignore_errors=True)

        probe = build_sandbox(self._config, sandbox_root, scenario.start_at)
        if fingerprint_store(probe.store)["fingerprint"] == expected_fp:
            # 工作沙箱与期望终态一致：直接复用，仅据事件流恢复时钟游标。
            platform = probe
            tick = _tick_from_events(eventlog)
            clock_at = scenario.start_at + tick * scenario.tick_seconds
        else:
            # 工作沙箱缺失/损坏：把临时重放结果就地扶正到工作沙箱目录。
            replay = replay_events(
                self._config,
                self._snapshots,
                metadata["snapshot_id"],
                sandbox_root,
                mutating,
                scenario,
                tick_seconds=scenario.tick_seconds,
            )
            platform = replay["platform"]
            tick = replay["tick"]
            clock_at = replay["clock_at"]
            # 封存会话只读：重建仅供查询，不再写恢复审计（会被封存规则拒绝）。
            if metadata.get("status") == STATUS_OPEN:
                self._audit.record(
                    "session_recovered", metadata["session_id"], restored_to=len(mutating)
                )

        CheckpointStore(session_dir).save(
            Checkpoint(
                event_count=len(eventlog.events),
                tick=tick,
                clock_at=clock_at,
                state_fingerprint=expected_fp,
            )
        )
        sim = Simulator(platform, scenario)
        sim._tick = tick  # noqa: SLF001
        platform.clock._now = clock_at  # noqa: SLF001
        platform.clock._mono = tick * scenario.tick_seconds  # noqa: SLF001
        return platform, sim

    def _refresh_checkpoint(self, ckpt_store: CheckpointStore, eventlog: ChainedEventLog, platform, sim) -> None:
        fp = fingerprint_store(platform.store)
        ckpt_store.save(
            Checkpoint(
                event_count=len(eventlog.events),
                tick=sim.tick,
                clock_at=platform.clock.now(),
                state_fingerprint=fp["fingerprint"],
            )
        )

    def _remember_context(self, ctx: dict) -> None:
        """动作追加事件后同步进程内缓存，使下一步免重放。"""

        eventlog = ctx["events"]
        self._context_cache[ctx["meta"]["session_id"]] = {
            "head": eventlog.head_hash,
            "length": len(eventlog.events),
            "platform": ctx["platform"],
            "sim": ctx["sim"],
            "meta": ctx["meta"],
        }

    @staticmethod
    def _find_prior(eventlog: ChainedEventLog, client_request_id: str):
        if not client_request_id:
            return None
        for event in eventlog.events:
            if event.kind == "operator_action":
                if event.payload.get("client_request_id") == client_request_id:
                    return event
        return None


def _tick_from_events(eventlog: ChainedEventLog) -> int:
    """从已重放的 sim_tick 事件恢复演练时钟游标（仅在复用工作沙箱时使用）。"""

    ticks = [event.sim_tick for event in eventlog.events if event.kind == "sim_tick"]
    return max(ticks, default=0)


def _action_view(event) -> dict:
    payload = event.payload
    result = payload.get("result")
    return {
        "action": payload.get("action"),
        "ok": result.get("ok") if isinstance(result, dict) else None,
        "violations": result.get("violations", []) if isinstance(result, dict) else [],
        "executed": payload.get("executed", False),
    }
