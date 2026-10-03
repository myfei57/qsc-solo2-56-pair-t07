"""演练服务：会话生命周期、世界推进、崩溃恢复、封存与深度复核。

一个 :class:`DrillService` 同时管多场并发演练：每个会话有独立目录、独立
``ManualClock``、独立分叉平台、独立哈希链、独立会话锁；彼此零共享，因此并发
演练互不串味。现场平台（``live_platform``）只在 shadow 拍快照和只读采样时被
触及，演练动作从不调用它。

时间：演练统一用 :class:`~psdline.clock.ManualClock`。replay 靠命令行/测试手动
:meth:`tick`；shadow 由一个后台驱动线程按真实墙钟 1:1 推进（可加速）。两种模式
跑同一份确定性引擎，差别只在数据来源和谁来敲钟。

崩溃安全：处置动作"先落意图（request）→ fsync → 执行 → 落结果（result）→
fsync"。若在执行中崩溃，恢复后该请求有意图无结果，会话被置 SUSPENDED 并标记
``recovered``，**绝不自动重发**（可能已在现场生效），由值班员确认后手动决定；
事件链不断、真相不丢。

历史不可变：封存时算评估、写只读报告、冻结哈希链；终态目录只可读。深度复核
从零重放事件，逐动作比对规范化结果与世界指纹，任何对历史的篡改都会现形。
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid

from ..app import Platform, build_platform
from ..clock import ManualClock
from ..config import PlatformConfig, default_config, with_storage_root
from ..store import JsonStore
from . import model
from .crypto import content_hash
from .dispatch import TRAINEE_ACTIONS, dispatch
from .errors import (
    DrillError,
    GuardRejected,
    LogIntegrityError,
    SessionNotFoundError,
    SessionStateError,
)
from .evaluate import Evaluation, evaluate, next_recommendation
from .fork import SnapshotStore, world_fingerprint
from .guard import BoundedStore, EgressGuard, ReadOnlyLiveView, assert_isolated_fork
from .log import DrillEventLog, normalize_result
from .model import (
    ABANDONED,
    ARMED,
    EVT_ACTION,
    EVT_INJECTION,
    EVT_STATE,
    EVT_SYSTEM,
    EVT_TICK,
    MODE_REPLAY,
    MODE_SHADOW,
    PENDING,
    RUNNING,
    SEALED,
    SUSPENDED,
)
from .registry import REPORT_FILE, SessionRegistry, locked_session
from .scenarios import get_scenario

SIM_START = 1_758_000_000.0   # 与 ManualClock 默认一致，保证跨会话可复现
IN_FLIGHT_MARKER = "__in_flight__"


class SessionRuntime:
    """一场正在本进程内打开的演练的全部活状态。"""

    def __init__(self, session_id: str, clock: ManualClock, platform: Platform,
                 log: DrillEventLog, guard: EgressGuard) -> None:
        self.session_id = session_id
        self.clock = clock
        self.platform = platform
        self.log = log
        self.guard = guard
        self.problem: dict = {}
        self.inject_time: float = 0.0
        self.driver_started = False
        self.driver_stop = threading.Event()
        self.driver_thread: threading.Thread | None = None
        self.live_samples: list[dict] = []


class DrillService:
    """应急演练服务。"""

    def __init__(
        self,
        drill_root: str,
        live_platform: Platform,
        config: PlatformConfig | None = None,
        *,
        shadow_speed: float = 1.0,
        tick_dt: float = 1.0,
    ) -> None:
        self._drill_root = os.path.abspath(drill_root)
        os.makedirs(self._drill_root, exist_ok=True)
        self._live = live_platform
        self._config = (config or live_platform.config or default_config())
        self._registry = SessionRegistry(self._drill_root)
        self._snapshots = SnapshotStore(self._drill_root, live_platform.clock)
        self._live_view = ReadOnlyLiveView(live_platform.store)
        self._runtimes: dict[str, SessionRuntime] = {}
        self._rlock = threading.RLock()
        self._shadow_speed = max(0.0, float(shadow_speed))
        self._tick_dt = float(tick_dt)

    # ---- 基础属性 -----------------------------------------------------

    @property
    def root(self) -> str:
        return self._drill_root

    @property
    def registry(self) -> SessionRegistry:
        return self._registry

    @property
    def snapshots(self) -> SnapshotStore:
        return self._snapshots

    def list_sessions(self) -> list[dict]:
        return self._registry.list_sessions()

    def catalog(self) -> list[dict]:
        from .scenarios import scenario_catalog

        return scenario_catalog()

    # ---- 创建 / 准备 / 开始 ------------------------------------------

    def create_session(
        self,
        scenario_key: str,
        actor: str,
        mode: str,
        *,
        title: str = "",
        snapshot_digest: str = "",
        target: dict | None = None,
    ) -> dict:
        """登记会话。shadow 自动拍实时快照；replay 必须指定一个既有快照。"""

        scenario = get_scenario(scenario_key)
        if mode not in model.DRILL_MODES:
            raise SessionStateError(f"未知演练模式: {mode}")

        if mode == MODE_SHADOW:
            bundle = self._snapshots.capture_from_live(self._live.store, source="live")
            snapshot_digest = bundle.digest
            source_meta = {"captured_at": bundle.captured_at, "source": "live"}
        else:
            if not snapshot_digest or not self._snapshots.load(snapshot_digest):
                raise SessionStateError(
                    "replay 模式必须指定一个已存在的故障时刻快照（snapshot_digest）",
                    component="drill-service",
                )
            loaded = self._snapshots.load(snapshot_digest)
            source_meta = {"captured_at": loaded.get("captured_at", ""),
                           "source": loaded.get("source", "")}

        meta = self._registry.create(
            mode=mode,
            scenario=scenario_key,
            actor=actor,
            snapshot_digest=snapshot_digest,
            title=title,
            metadata={"source": source_meta, "target_override": target or {}},
        )
        return meta

    def prepare(self, session_id: str) -> dict:
        """把快照还原成分叉平台、摆好故障前世界、记录 setup，状态转 ARMED。

        setup 动作也进事件链（kind=injection/setup），这样重放时世界从零可重建。
        """

        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, PENDING)
            fork_root = self._registry.fork_dir(session_id)
            assert_isolated_fork(fork_root, self._live.store.root)
            self._snapshots.fork_to(meta["snapshot_digest"], fork_root)

            clock = ManualClock(SIM_START)
            bounded = BoundedStore(fork_root)
            fork_config = with_storage_root(self._config, fork_root)
            # 注入边界存储：演练平台的所有写都被限定在分叉目录内。
            platform = Platform(fork_config, clock=clock,
                                unit=f"DRILL/{session_id}", store=bounded)
            log = DrillEventLog(self._registry.session_dir(session_id), clock)
            guard = EgressGuard()
            guard.arm(session_id, [fork_root])
            runtime = SessionRuntime(session_id, clock, platform, log, guard)

            scenario = get_scenario(meta["scenario"])
            problem = copy.deepcopy(dict(scenario.target))
            problem.update(dict(meta.get("metadata", {}).get("target_override") or {}))
            runtime.problem = problem

            # setup 在故障零点之前；先记 setup 计划，再在分叉平台真实执行。
            # 深拷贝题面：后续 inject 会原地补全 psd_id 等字段，事件载荷必须定格。
            log.append(EVT_INJECTION, {
                "phase": "setup",
                "scenario": scenario.key,
                "problem": copy.deepcopy(problem),
            }, actor="system")
            scenario.setup(platform, problem)
            state_hash = world_fingerprint(bounded)
            log.append(EVT_STATE, {"phase": "armed", "world_hash": state_hash,
                                   "mode": meta["mode"]}, actor="system")
            self._put_runtime(runtime)
            new_meta = self._registry.transition(session_id, ARMED, problem=problem)
            return {"meta": new_meta, "world_hash": state_hash,
                    "briefing": scenario.briefing, "problem": problem}

    def start(self, session_id: str, actor: str) -> dict:
        """上膛 → 运行：注入故障（进哈希链），shadow 模式启动实时驱动。"""

        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, ARMED)
            runtime = self._require_runtime(session_id)
            scenario = get_scenario(meta["scenario"])

            request_id = self._new_request_id()
            runtime.log.append(EVT_INJECTION, {
                "phase": "request", "request_id": request_id,
                "scenario": scenario.key, "actor": actor,
            }, actor=actor)
            injection = scenario.inject(runtime.platform, runtime.problem)
            runtime.inject_time = runtime.clock.now() - SIM_START
            world_hash = world_fingerprint(runtime.platform.store)
            runtime.log.append(EVT_INJECTION, {
                "phase": "result", "request_id": request_id,
                "injection": injection, "world_hash": world_hash,
                "inject_time": runtime.inject_time,
            }, actor=actor)
            new_meta = self._registry.transition(
                session_id, RUNNING,
                inject_time=runtime.inject_time, problem=runtime.problem,
            )
            if meta["mode"] == MODE_SHADOW and self._shadow_speed > 0:
                self._start_driver(session_id)
            return {"meta": new_meta, "injection": injection,
                    "inject_time": runtime.inject_time, "world_hash": world_hash}

    # ---- 处置动作 -----------------------------------------------------

    def act(self, session_id: str, action: str, params: dict, actor: str,
            *, request_id: str | None = None) -> dict:
        """值班员下发一个处置动作：意图先落盘，再在分叉平台真执行。"""

        if action not in TRAINEE_ACTIONS:
            raise DrillError(f"值班员动作不在允许集合: {action}", component="drill-service")
        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, RUNNING)
            runtime = self._require_runtime(session_id)

            request_id = request_id or self._new_request_id()
            # 出口门控：执行前确认目标仍在演练分叉域内（fail-closed）。
            runtime.guard.check_egress(session_id, runtime.platform.store.root, phase="pre")
            runtime.log.append(EVT_ACTION, {
                "phase": "request", "request_id": request_id,
                "action": action, "params": params, "actor": actor,
            }, actor=actor)

            result = dispatch(runtime.platform, action, params)
            world_hash = world_fingerprint(runtime.platform.store)
            runtime.guard.check_egress(session_id, runtime.platform.store.root, phase="post")
            runtime.log.append(EVT_ACTION, {
                "phase": "result", "request_id": request_id,
                "action": action, "request": {"action": action, **params},
                "result": result, "world_hash": world_hash,
            }, actor=actor)
            count = int(meta.get("request_count", 0)) + 1
            self._registry.update_meta(
                session_id, request_count=count,
                sim_time=runtime.clock.now() - SIM_START,
            )
            return {"request_id": request_id, "result": result,
                    "world_hash": world_hash, "sim_time": runtime.clock.now() - SIM_START}

    def hint(self, session_id: str, actor: str) -> dict:
        """请求下一步标准动作提示（请求本身进审计，提示不替人做决定）。"""

        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, RUNNING)
            runtime = self._require_runtime(session_id)
            evaluation = self._evaluate_current(meta, runtime)
            scenario = get_scenario(meta["scenario"])
            advice = next_recommendation(scenario, evaluation)
            runtime.log.append(EVT_SYSTEM, {"phase": "hint", "advice": advice}, actor=actor)
            return advice

    # ---- 世界推进 -----------------------------------------------------

    def tick(self, session_id: str, dt: float | None = None, *, actor: str = "system") -> dict:
        """推进演练世界一个步长：演练时钟前进 + 平台跑一个控制周期。

        replay 由调用方手动敲；shadow 由驱动线程按墙钟调用。推进的是**演练时钟**，
        现场时钟不动。周期里位置失准等现象会自然形成，无需特殊造假。
        """

        dt = self._tick_dt if dt is None else float(dt)
        if dt <= 0:
            raise DrillError("步长必须为正", component="drill-service")
        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, RUNNING)
            runtime = self._require_runtime(session_id)
            runtime.clock.advance(dt)
            cycle = runtime.platform.cycle(dt=dt)
            world_hash = world_fingerprint(runtime.platform.store)
            runtime.log.append(EVT_TICK, {
                "dt": dt, "sim_time": runtime.clock.now() - SIM_START,
                "stale_positions": cycle.get("stale_positions", []),
                "locked_count": cycle.get("locked_count", 0),
                "world_hash": world_hash,
            }, actor=actor)
            ticks = int(meta.get("tick_count", 0)) + 1
            self._registry.update_meta(
                session_id, tick_count=ticks,
                sim_time=runtime.clock.now() - SIM_START,
            )
            return {"sim_time": runtime.clock.now() - SIM_START,
                    "cycle": cycle, "world_hash": world_hash}

    def suspend(self, session_id: str, actor: str, reason: str = "") -> dict:
        """挂起演练（冻结世界，驱动停转），用于恢复或教员冻结。"""

        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, (RUNNING, ARMED))
            self._stop_driver(session_id)
            runtime = self._get_runtime(session_id)
            if runtime is not None:
                runtime.log.append(EVT_STATE, {"phase": "suspend", "reason": reason},
                                   actor=actor)
            return self._registry.transition(session_id, SUSPENDED)

    def resume(self, session_id: str, actor: str) -> dict:
        """从挂起恢复（崩溃恢复后也走这里，需值班员显式确认）。"""

        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, SUSPENDED)
            runtime = self._open_runtime(session_id, meta)
            runtime.log.assert_intact()
            runtime.log.append(EVT_STATE, {"phase": "resume", "actor": actor}, actor=actor)
            new_meta = self._registry.transition(session_id, RUNNING)
            if new_meta["mode"] == MODE_SHADOW and self._shadow_speed > 0:
                self._start_driver(session_id)
            return new_meta

    # ---- 终态：封存 / 破玻 -------------------------------------------

    def seal(self, session_id: str, actor: str) -> dict:
        """正常结束：算评估、写不可变报告、冻结哈希链。"""

        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, (RUNNING, SUSPENDED))
            self._stop_driver(session_id)
            runtime = self._open_runtime(session_id, meta)
            runtime.log.assert_intact()
            evaluation = self._evaluate_current(meta, runtime)
            end_world = world_fingerprint(runtime.platform.store)
            runtime.log.append(EVT_STATE, {"phase": "seal", "world_hash": end_world},
                               actor=actor)
            tip = runtime.log.tip_hash
            report = self._build_report(meta, runtime, evaluation, tip, end_world)
            self._write_report(session_id, report)
            runtime.log.freeze()
            runtime.guard.disarm()
            new_meta = self._registry.transition(
                session_id, SEALED, termination_reason="sealed",
                final_world_hash=end_world, chain_tip=tip, score=evaluation.score,
                passed=evaluation.passed,
            )
            self._discard_runtime(session_id)
            return {"meta": new_meta, "report": report}

    def break_glass(self, session_id: str, actor: str, confirmer: str, reason: str) -> dict:
        """破玻退出：双人确认，不可逆，会话转 ABANDONED，历史照样冻结保全。

        演练期间禁止切回实作；这是唯一出口。必须两个不同的人确认（值班员 +
        复核人），防止单人误触/擅退。退出的是演练工作站——现场平台从不被本
        服务触碰，因此破玻不影响现场任何状态。
        """

        if not actor or not confirmer or actor == confirmer:
            raise SessionStateError("破玻必须由两名不同人员确认", component="drill-service")
        with locked_session(self._registry, session_id):
            meta = self._registry.get(session_id)
            self._require_state(meta, (ARMED, RUNNING, SUSPENDED, PENDING))
            self._stop_driver(session_id)
            runtime = self._get_runtime(session_id)
            if runtime is not None:
                runtime.log.append(EVT_STATE, {
                    "phase": "break_glass", "actor": actor, "confirmer": confirmer,
                    "reason": reason,
                }, actor=actor)
                tip = runtime.log.tip_hash
                runtime.log.freeze()
                runtime.guard.disarm()
            else:
                tip = ""
            new_meta = self._registry.transition(
                session_id, ABANDONED, termination_reason=f"break_glass: {reason}",
                chain_tip=tip,
            )
            self._discard_runtime(session_id)
            return new_meta

    # ---- 恢复（崩溃/重启） -------------------------------------------

    def recover_all(self) -> list[dict]:
        """服务启动时扫描：处理上次崩溃留下的所有会话。

        - 名册与目录不一致：重建名册；
        - RUNNING 且事件链完整：转 SUSPENDED 并标记 recovered，等待值班员显式
          resume（不自动继续，避免驱动时间凭空流逝）；
        - ARMED 尚未注入：保持 ARMED，可重新 start；
        - PENDING 连分叉都没建好：fail-closed 直接放弃，杜绝半初始化会话复活；
        - 最后一个动作"有意图无结果"：记 in_flight，绝不自动重发。
        """

        self._registry.rebuild_index()
        results = []
        for meta in self._registry.scan_all():
            state = meta.get("state")
            if state not in (RUNNING, ARMED, PENDING, SUSPENDED):
                continue
            session_id = str(meta["id"])
            try:
                outcome = self._recover_one(session_id, meta)
                results.append({"session": session_id, "state": state,
                                "recovered": True, "outcome": outcome})
            except LogIntegrityError as error:
                results.append({"session": session_id, "recovered": False,
                                "error": error.message})
        return results

    def _recover_one(self, session_id: str, meta: dict) -> str:
        with locked_session(self._registry, session_id):
            current = self._registry.read_meta(session_id)
            if current is None or current.get("state") in model.TERMINAL_STATES:
                return "terminal"
            if current.get("recovered"):
                return "already-recovered"
            state = current.get("state")
            if state == PENDING:
                # 连分叉平台都没建成，无法可信恢复，fail-closed 放弃（历史保全）。
                log_path = os.path.join(self._registry.session_dir(session_id), "events.jsonl")
                if os.path.exists(log_path):
                    DrillEventLog(self._registry.session_dir(session_id),
                                  ManualClock(SIM_START)).freeze()
                self._registry.transition(session_id, ABANDONED,
                                          termination_reason="crash-at-pending: 未完成初始化，安全放弃")
                return "abandoned"
            log = DrillEventLog(self._registry.session_dir(session_id),
                                ManualClock(SIM_START))
            broken = log.verify()
            if broken is not None:
                raise LogIntegrityError(
                    f"会话 {session_id} 哈希链断裂于第 {broken.seq} 条",
                    component="drill-log",
                )
            in_flight = self._detect_in_flight(log)
            note = "崩溃恢复：事件链完整" + (
                f"；检测到未完成动作 {in_flight}（已挂起，未自动重发）" if in_flight
                else "；无未完成动作"
            )
            log.append(EVT_SYSTEM, {
                "phase": "recovered", "in_flight": in_flight, "note": note,
            }, actor="system")
            if state == RUNNING:
                self._registry.transition(session_id, SUSPENDED,
                                          recovered=True, recovery_note=note)
                return "suspended"
            # ARMED：尚未注入故障，保持上膛，允许重新开始。
            self._registry.update_meta(session_id, recovered=True, recovery_note=note)
            return "armed"

    def _detect_in_flight(self, log: DrillEventLog) -> str:
        """找出最后一个"有 request 无 result"的动作/注入请求。"""

        from .log import iter_paired

        paired = iter_paired(log.records())
        for request, result in reversed(paired):
            if result is None:
                return str(request.get("request_id", ""))
        return ""

    # ---- 深度复核（从事件链重放） -------------------------------------

    def verify_session(self, session_id: str) -> dict:
        """终态会话深度复核：哈希链 + 从零重放 + 逐动作结果比对 + 世界指纹。"""

        meta = self._registry.get(session_id)
        if meta.get("state") not in model.TERMINAL_STATES:
            raise SessionStateError("只能对终态会话做深度复核", component="drill-service")

        log = DrillEventLog(self._registry.session_dir(session_id), ManualClock(SIM_START))
        log.assert_intact()

        # 用一个全新的分叉平台从事件链重放，不读当初的 fork 目录。
        replay_fork = os.path.join(self._registry.session_dir(session_id), "replay-verify")
        self._snapshots.fork_to(meta["snapshot_digest"], replay_fork)
        clock = ManualClock(SIM_START)
        bounded = BoundedStore(replay_fork)
        config = with_storage_root(self._config, replay_fork)
        platform = Platform(config, clock=clock, unit=f"VERIFY/{session_id}", store=bounded)

        scenario = get_scenario(meta["scenario"])
        problem = dict(meta.get("problem") or scenario.target)
        mismatches: list[dict] = []
        action_events: list[dict] = []
        inject_time = 0.0

        for record in log.records():
            payload = record.payload
            phase = payload.get("phase")
            if record.kind == EVT_INJECTION and phase == "setup":
                scenario.setup(platform, dict(payload.get("problem", problem)))
            elif record.kind == EVT_INJECTION and phase == "result":
                # 重放注入动作本身（与原世界收敛到一致的故障态）。
                scenario.inject(platform, problem)
                inject_time = float(payload.get("inject_time", 0.0))
            elif record.kind == EVT_TICK:
                clock.advance(float(payload.get("dt", 1.0)))
                platform.cycle(dt=float(payload.get("dt", 1.0)))
            elif record.kind in (EVT_ACTION, EVT_INJECTION) and phase == "result":
                # 把当时的执行结果与"现在重新执行"的规范化结果比对。
                recorded = normalize_result(dict(payload.get("result") or {}))
                if record.kind == EVT_ACTION:
                    request = dict(payload.get("request") or {})
                    action = str(request.pop("action", ""))
                    fresh = normalize_result(dispatch(platform, action, request))
                    if fresh != recorded:
                        mismatches.append({
                            "seq": record.seq, "action": action,
                            "recorded": recorded, "replayed": fresh,
                        })
                    action_events.append({
                        "seq": record.seq,
                        "sim_time": record.sim_time,
                        "request": payload.get("request"),
                        "result": payload.get("result"),
                    })

        # 世界指纹：重放最终世界应与封存时一致（确定性）。
        replay_hash = world_fingerprint(bounded)
        final_hash = str(meta.get("final_world_hash", ""))
        world_ok = replay_hash == final_hash

        fresh_eval = evaluate(
            scenario, problem, action_events, SIM_START + inject_time,
            end_time=SIM_START + float(meta.get("sim_time", inject_time)),
        )
        recorded_report = self._read_report(session_id)
        score_ok = (
            recorded_report is not None
            and abs(float(recorded_report.get("evaluation", {}).get("score", -1))
                    - fresh_eval.score) < 0.05
        )

        # 清理临时重放目录（复核不留可变副本）。
        import shutil

        shutil.rmtree(replay_fork, ignore_errors=True)

        return {
            "session": session_id,
            "chain_intact": log.verify() is None,
            "events": log.count,
            "action_mismatches": mismatches,
            "world_hash_match": world_ok,
            "recorded_world_hash": final_hash,
            "replayed_world_hash": replay_hash,
            "score_reproducible": score_ok,
            "replayed_score": fresh_eval.score,
            "passed": (
                log.verify() is None and not mismatches and world_ok and score_ok
            ),
        }

    # ---- 查询 / 现场对照（shadow） -----------------------------------

    def world_view(self, session_id: str) -> dict:
        """返回演练世界当前快照 + （shadow）此刻真实现场的只读对照。"""

        meta = self._registry.get(session_id)
        runtime = self._get_runtime(session_id)
        if runtime is None:
            return {"state": meta.get("state"), "platform": None}
        view = {
            "state": meta.get("state"),
            "sim_time": runtime.clock.now() - SIM_START,
            "mode": meta.get("mode"),
            "platform": runtime.platform.snapshot(),
        }
        if meta.get("mode") == MODE_SHADOW:
            view["live_now"] = self.sample_live()
        return view

    def sample_live(self) -> dict:
        """只读采样真实现场当前态（绝不写现场）。"""

        live_status = self._live.status()
        return {
            "captured_at": self._live.clock.utc_iso(),
            "operations": live_status.get("operations"),
            "open_gates": live_status.get("open_gates"),
            "interlock": live_status.get("interlock"),
            "trains": live_status.get("trains"),
            "document_count": len(self._live_view.list_ids()),
        }

    def report(self, session_id: str) -> dict:
        report = self._read_report(session_id)
        if report is None:
            raise SessionNotFoundError(f"会话尚未封存，没有复盘报告: {session_id}")
        return report

    def timeline(self, session_id: str, limit: int = 500) -> list[dict]:
        """返回会话事件链（复盘用）：每步动作、结果、世界指纹、时间。"""

        log = DrillEventLog(self._registry.session_dir(session_id), ManualClock(SIM_START))
        items = []
        for record in log.records()[-limit:]:
            items.append({
                "seq": record.seq,
                "sim_time": round(record.sim_time - SIM_START, 3),
                "kind": record.kind,
                "actor": record.actor,
                "payload": record.payload,
                "hash": record.hash[:12],
            })
        return items

    # ---- 内部工具 -----------------------------------------------------

    def _evaluate_current(self, meta: dict, runtime: SessionRuntime) -> Evaluation:
        scenario = get_scenario(meta["scenario"])
        action_events = []
        for record in runtime.log.records():
            if record.kind == EVT_ACTION and record.payload.get("phase") == "result":
                action_events.append({
                    "seq": record.seq,
                    "sim_time": record.sim_time,  # 绝对仿真秒（含 SIM_START）
                    "request": record.payload.get("request"),
                    "result": record.payload.get("result"),
                })
        return evaluate(
            scenario, runtime.problem, action_events,
            SIM_START + runtime.inject_time,
        )

    def _build_report(self, meta: dict, runtime: SessionRuntime,
                      evaluation: Evaluation, chain_tip: str,
                      world_hash: str) -> dict:
        scenario = get_scenario(meta["scenario"])
        guard_blocks = runtime.guard.rejections
        return {
            "session_id": meta["id"],
            "title": meta.get("title", ""),
            "scenario": meta["scenario"],
            "scenario_name": scenario.name,
            "mode": meta["mode"],
            "actor": meta.get("actor", ""),
            "created_at": meta.get("created_at", ""),
            "sealed_at": meta.get("updated_at", ""),
            "problem": runtime.problem,
            "snapshot_digest": meta.get("snapshot_digest", ""),
            "duration_s": round(runtime.clock.now() - SIM_START, 1),
            "evaluation": evaluation.as_dict(),
            "guard": {
                "egress_rejections": guard_blocks,
                "isolation": "fork-copy + bounded-store + egress-guard",
            },
            "final_world_hash": world_hash,
            "chain_tip": chain_tip,
            "chain_events": runtime.log.count,
        }

    def _write_report(self, session_id: str, report: dict) -> str:
        path = self._registry.report_path(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            import json

            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o444)
        return path

    def _read_report(self, session_id: str) -> dict | None:
        path = self._registry.report_path(session_id)
        if not os.path.exists(path):
            return None
        import json

        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    # ---- 运行时缓存 / 打开 -------------------------------------------

    def _put_runtime(self, runtime: SessionRuntime) -> None:
        with self._rlock:
            self._runtimes[runtime.session_id] = runtime

    def _get_runtime(self, session_id: str) -> SessionRuntime | None:
        with self._rlock:
            return self._runtimes.get(session_id)

    def _require_runtime(self, session_id: str) -> SessionRuntime:
        runtime = self._get_runtime(session_id)
        if runtime is None:
            # 同进程内被挂起/驱逐后重新打开
            meta = self._registry.get(session_id)
            runtime = self._open_runtime(session_id, meta)
        return runtime

    def _open_runtime(self, session_id: str, meta: dict) -> SessionRuntime:
        """从落盘状态重新打开一个会话运行时（fork 目录即世界，日志即历史）。"""

        cached = self._get_runtime(session_id)
        if cached is not None:
            return cached
        fork_root = self._registry.fork_dir(session_id)
        # 重开时钟取"元数据秒数"与"事件链尾秒数"的较大者，保证时间单调。
        log_probe = DrillEventLog(self._registry.session_dir(session_id),
                                  ManualClock(SIM_START))
        tail_time = max((r.sim_time for r in log_probe.records()), default=SIM_START)
        base = max(SIM_START + float(meta.get("sim_time", 0.0)), tail_time)
        clock = ManualClock(base)
        bounded = BoundedStore(fork_root)
        config = with_storage_root(self._config, fork_root)
        platform = Platform(config, clock=clock, unit=f"DRILL/{session_id}", store=bounded)
        log = DrillEventLog(self._registry.session_dir(session_id), clock)
        guard = EgressGuard()
        guard.arm(session_id, [fork_root])
        runtime = SessionRuntime(session_id, clock, platform, log, guard)
        runtime.problem = dict(meta.get("problem") or {})
        runtime.inject_time = float(meta.get("inject_time", 0.0))
        self._put_runtime(runtime)
        return runtime

    def _discard_runtime(self, session_id: str) -> None:
        with self._rlock:
            self._runtimes.pop(session_id, None)

    # ---- 后台驱动（shadow 实时） -------------------------------------

    def _start_driver(self, session_id: str) -> None:
        runtime = self._require_runtime(session_id)
        if runtime.driver_started:
            return
        runtime.driver_stop.clear()

        def loop() -> None:
            period = self._tick_dt / max(self._shadow_speed, 1e-9)
            while not runtime.driver_stop.wait(period):
                try:
                    self.tick(session_id, self._tick_dt, actor="shadow-driver")
                except (SessionStateError, LogIntegrityError, GuardRejected):
                    break  # 会话终态/挂起/被封，驱动自行退出

        thread = threading.Thread(target=loop, name=f"drill-driver-{session_id}",
                                  daemon=True)
        runtime.driver_thread = thread
        runtime.driver_started = True
        thread.start()

    def _stop_driver(self, session_id: str) -> None:
        runtime = self._get_runtime(session_id)
        if runtime is None:
            return
        runtime.driver_stop.set()
        thread = runtime.driver_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        runtime.driver_started = False
        runtime.driver_thread = None

    def shutdown(self) -> None:
        """停止全部驱动线程（不封存在演会话——留给恢复流程处理）。"""

        for session_id in list(self._runtimes):
            self._stop_driver(session_id)

    # ---- 小工具 -------------------------------------------------------

    def _require_state(self, meta: dict, allowed: str | tuple[str, ...]) -> None:
        allowed_set = {allowed} if isinstance(allowed, str) else set(allowed)
        state = str(meta.get("state", ""))
        if state not in allowed_set:
            raise SessionStateError(
                f"会话 {meta.get('id')} 当前状态 {state}，要求 {sorted(allowed_set)}",
                component="drill-service",
            )

    @staticmethod
    def _new_request_id() -> str:
        return f"req-{uuid.uuid4().hex[:12]}"
