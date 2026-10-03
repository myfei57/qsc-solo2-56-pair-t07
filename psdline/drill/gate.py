"""演练门控：演练指令的唯一放行点（fail-closed）。

设计目标是「一条现场动作都不许真发出去」，并用纵深防御而不是口头约定来保证：

1. **模式隔离**：门只有两种模式——``LIVE``（现场）与 ``DRILL``（演练）。
   ``DRILL`` 模式下任何指令都不会转发给现场出口，直接判 ``denied:drill_mode``，
   并落一条演练拦截审计。
2. **令牌与会话校验**：演练指令必须携带由本服务 HMAC 签发、绑定 ``session_id``
   与快照的防篡改令牌；令牌缺失、串会话、过期一律拒绝。
3. **现场出口独立**：:class:`FieldGateway` 是唯一会触达真实设备的出口，门在
   ``DRILL`` 模式下根本不调用它。现场真作走另一个独立门实例与独立审计流。
4. **默认关闭**：门构造、策略文件缺失、令牌校验异常时一律拒绝（fail-closed），
   绝不因为「判断不出来」而放行。

门只做「能不能发」，不做业务动作；业务动作在演练引擎的沙箱里推演。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Callable

from ..errors import RailError

POLICY_VERSION = "drill-gate-v1"
LIVE = "live"
DRILL = "drill"

DENY_DRILL_MODE = "drill_mode"
DENY_BAD_TOKEN = "bad_token"
DENY_SESSION_MISMATCH = "session_mismatch"
DENY_SESSION_CLOSED = "session_closed"
DENY_UNKNOWN_ACTION = "unknown_action"
DENY_BAD_ENVELOPE = "bad_envelope"
DENY_POLICY = "policy_violation"


class GateError(RailError):
    """门控配置或调用错误。"""


# 演练允许推演的现场动作白名单（只在沙箱内生效，绝不触达现场）。
DRILL_ACTION_WHITELIST = frozenset(
    {
        "arrive",
        "depart",
        "doors_open",
        "check_gap",
        "probe_gap",
        "enqueue",
        "process_queue",
        "set_fault",
        "clear_fault",
        "batch_open",
        "sweep",
        "arm",
        "refresh_position",
        "renumber",
        "sequence_verdict",
        "register_dwell",
        "assign_route",
        "cycle",
    }
)


@dataclass(frozen=True)
class CommandEnvelope:
    """一条经过门控的指令封套。"""

    action: str
    mode: str
    session_id: str
    token: str
    args: dict
    client_request_id: str
    issued_at: float

    @classmethod
    def of(cls, payload: dict) -> "CommandEnvelope":
        if not isinstance(payload, dict):
            raise GateError("指令封套必须是对象", component="drill.gate")
        try:
            return cls(
                action=str(payload["action"]),
                mode=str(payload.get("mode", "")),
                session_id=str(payload.get("session_id", "")),
                token=str(payload.get("token", "")),
                args=dict(payload.get("args", {})),
                client_request_id=str(payload.get("client_request_id", "")),
                issued_at=float(payload.get("issued_at", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GateError(f"非法指令封套: {error}", component="drill.gate") from error


class DrillTokenSigner:
    """对演练令牌做 HMAC-SHA256 签发与校验。

    密钥来自环境变量 ``PSDLINE_DRILL_HMAC``；未显式注入时在进程内生成一次性
    密钥（跨进程即失效，天然防止把旧令牌拿到重启后的服务上重放）。生产部署应
    通过受管密钥注入环境变量。
    """

    def __init__(self, secret: bytes | None = None) -> None:
        if secret is None:
            env = os.environ.get("PSDLINE_DRILL_HMAC", "")
            secret = env.encode("utf-8") if env else os.urandom(32)
        if len(secret) < 16:
            raise GateError("演练 HMAC 密钥长度不足（至少 16 字节）", component="drill.gate")
        self._secret = secret

    def issue(self, session_id: str, snapshot_id: str, *, ttl_s: float = 24 * 3600.0) -> dict:
        issued = time.time()
        expires = issued + ttl_s
        body = f"{session_id}|{snapshot_id}|{issued:.3f}|{expires:.3f}"
        mac = hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"token": f"{body}|{mac}", "issued_at": issued, "expires_at": expires}

    def verify(self, token: str, session_id: str, snapshot_id: str, *, now: float | None = None) -> tuple[bool, str]:
        parts = token.split("|")
        if len(parts) != 5:
            return False, DENY_BAD_TOKEN
        sid, snap_id, issued_raw, expires_raw, mac = parts
        body = f"{sid}|{snap_id}|{issued_raw}|{expires_raw}"
        expected = hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return False, DENY_BAD_TOKEN
        if sid != session_id:
            return False, DENY_SESSION_MISMATCH
        if snap_id != snapshot_id:
            return False, DENY_SESSION_MISMATCH
        try:
            expires = float(expires_raw)
        except ValueError:
            return False, DENY_BAD_TOKEN
        moment = time.time() if now is None else now
        if moment > expires:
            return False, DENY_BAD_TOKEN
        return True, "ok"


@dataclass
class GateVerdict:
    """门控判定结果。"""

    allowed: bool
    reason: str
    transmitted: bool
    policy_version: str
    mode: str
    detail: dict

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "transmitted": self.transmitted,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "detail": dict(self.detail),
        }


AuditSink = Callable[[str, dict], None]


class CommandGate:
    """按模式与令牌决定指令能否离开控制层。"""

    def __init__(
        self,
        mode: str,
        signer: DrillTokenSigner,
        *,
        is_session_open: Callable[[str], bool] | None = None,
        snapshot_of: Callable[[str], str] | None = None,
        audit: AuditSink | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if mode not in (LIVE, DRILL):
            raise GateError(f"未知门控模式: {mode}", component="drill.gate")
        self._mode = mode
        self._signer = signer
        self._is_open = is_session_open or (lambda _sid: False)
        self._snapshot_of = snapshot_of or (lambda _sid: "")
        self._audit = audit or (lambda _kind, _detail: None)
        self._clock = clock or time.time

    @property
    def mode(self) -> str:
        return self._mode

    def screen(self, envelope: CommandEnvelope) -> GateVerdict:
        """判定一条指令：演练模式永不 ``transmitted``。"""

        if envelope.mode != self._mode:
            return self._deny(envelope, DENY_POLICY, {"envelope_mode": envelope.mode})
        if not envelope.action or envelope.action not in DRILL_ACTION_WHITELIST:
            return self._deny(envelope, DENY_UNKNOWN_ACTION, {"action": envelope.action})
        if not envelope.session_id or not envelope.client_request_id:
            return self._deny(envelope, DENY_BAD_ENVELOPE, {})
        if not self._is_open(envelope.session_id):
            return self._deny(envelope, DENY_SESSION_CLOSED, {})
        snapshot_id = self._snapshot_of(envelope.session_id)
        ok, reason = self._signer.verify(
            envelope.token,
            envelope.session_id,
            snapshot_id,
            now=self._clock(),
        )
        if not ok:
            return self._deny(envelope, reason, {})

        # 演练模式：判定「本可放行」但绝不触达现场出口。
        if self._mode == DRILL:
            verdict = GateVerdict(
                allowed=True,
                reason="ok",
                transmitted=False,
                policy_version=POLICY_VERSION,
                mode=DRILL,
                detail={"note": "演练指令仅在沙箱推演，未触达现场出口"},
            )
            self._audit("drill_command_screened", {"envelope": _safe_envelope(envelope)})
            return verdict

        # 现场模式由现场门持有，演练服务不会构造这种封套。
        return self._deny(envelope, DENY_POLICY, {"note": "演练服务不具备现场放行路径"})

    def _deny(self, envelope: CommandEnvelope, reason: str, extra: dict) -> GateVerdict:
        verdict = GateVerdict(
            allowed=False,
            reason=reason,
            transmitted=False,
            policy_version=POLICY_VERSION,
            mode=self._mode,
            detail={"action": envelope.action, "session_id": envelope.session_id, **extra},
        )
        self._audit(
            "drill_command_denied",
            {"reason": reason, "envelope": _safe_envelope(envelope), **extra},
        )
        return verdict


def _safe_envelope(envelope: CommandEnvelope) -> dict:
    """审计里绝不记录完整令牌，只留指纹，避免泄露签名材料。"""

    token = envelope.token
    return {
        "action": envelope.action,
        "mode": envelope.mode,
        "session_id": envelope.session_id,
        "client_request_id": envelope.client_request_id,
        "token_fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "",
    }


class FieldGateway:
    """唯一的现场设备出口。

    演练门在 ``DRILL`` 模式下不会调用这里；它存在的意义是让「演练是否真的把动作
    发出去了」变成一个可断言、可计数的事实：演练跑完，``transmitted`` 必须为 0。
    """

    def __init__(self) -> None:
        self._transmitted: list[dict] = []

    def transmit(self, action: str, args: dict) -> dict:
        record = {"action": action, "args": dict(args)}
        self._transmitted.append(record)
        return {"delivered": True, "record": record}

    @property
    def transmitted_count(self) -> int:
        return len(self._transmitted)

    def transmitted_actions(self) -> list[str]:
        return [item["action"] for item in self._transmitted]
