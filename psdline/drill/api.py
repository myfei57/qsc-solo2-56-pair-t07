"""演练 HTTP 接口（挂在控制台 ``/api/drill/...`` 下）。

与现场接口刻意分开：

- 路径前缀 ``/api/drill``，响应体一律带会话标识与演练状态，不会被误当成现场
  应答；
- 演练自己的错误类型在这里翻译成 HTTP 状态：会话状态冲突 409、会话不存在 404、
  出口门控拒绝 403、参数错误 400、历史篡改 422；
- 所有处置接口只操作 :class:`~psdline.drill.service.DrillService`，控制台即使
  被人误把演练地址填进现场上位机，真正的现场接口也不会多出任何演练动作。
"""

from __future__ import annotations

from ..console.router import Router
from .errors import (
    DrillError,
    EvaluateError,
    GuardRejected,
    LogIntegrityError,
    SessionNotFoundError,
    SessionStateError,
)
from .service import DrillService


def _require(body: dict, key: str) -> str:
    value = body.get(key)
    if value is None or str(value) == "":
        raise EvaluateError(f"缺少必填字段 {key!r}")
    return str(value)


class DrillApi:
    """演练接口集合（自带路由表，由控制台按前缀委托）。"""

    PREFIX = "/api/drill"

    def __init__(self, service: DrillService) -> None:
        self._service = service
        self._router = Router()
        self._register()

    @property
    def service(self) -> DrillService:
        return self._service

    def routes(self) -> list[dict]:
        return [route.as_dict() for route in self._router.routes()]

    def _register(self) -> None:
        add = self._router.add
        add("GET", "/api/drill/scenarios", lambda p, b: {"scenarios": self._service.catalog()})
        add("GET", "/api/drill/snapshots", lambda p, b: {"snapshots": self._service.snapshots.list()})
        add("GET", "/api/drill/sessions", lambda p, b: {"sessions": self._service.list_sessions()})
        add("POST", "/api/drill/sessions", self._create)
        add("POST", "/api/drill/recover", lambda p, b: {"recovered": self._service.recover_all()})
        add("GET", "/api/drill/sessions/{sessionID}", self._meta)
        add("GET", "/api/drill/sessions/{sessionID}/world", self._world)
        add("GET", "/api/drill/sessions/{sessionID}/timeline", self._timeline)
        add("GET", "/api/drill/sessions/{sessionID}/report", self._report)
        add("POST", "/api/drill/sessions/{sessionID}/prepare", self._prepare)
        add("POST", "/api/drill/sessions/{sessionID}/start", self._start)
        add("POST", "/api/drill/sessions/{sessionID}/act", self._act)
        add("POST", "/api/drill/sessions/{sessionID}/tick", self._tick)
        add("POST", "/api/drill/sessions/{sessionID}/hint", self._hint)
        add("POST", "/api/drill/sessions/{sessionID}/suspend", self._suspend)
        add("POST", "/api/drill/sessions/{sessionID}/resume", self._resume)
        add("POST", "/api/drill/sessions/{sessionID}/seal", self._seal)
        add("POST", "/api/drill/sessions/{sessionID}/break-glass", self._break_glass)
        add("POST", "/api/drill/sessions/{sessionID}/verify", self._verify)

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        matched = self._router.match(method, path)
        if matched is None:
            return 404, {"error": "not_found", "path": path, "domain": "drill"}
        handler, params = matched
        merged = {**query, **params}
        try:
            return 200, handler(merged, body)
        except SessionNotFoundError as error:
            return 404, {"error": "session_not_found", **error.as_record()}
        except GuardRejected as error:
            # 出口门控拒绝是安全事件：403 + 明确标记，绝不伪装成普通冲突
            return 403, {"error": "egress_denied", **error.as_record()}
        except LogIntegrityError as error:
            return 422, {"error": "history_tampered", **error.as_record()}
        except SessionStateError as error:
            return 409, {"error": "session_state", **error.as_record()}
        except EvaluateError as error:
            return 400, {"error": "bad_drill_request", **error.as_record()}
        except DrillError as error:
            return 400, {"error": type(error).__name__, **error.as_record()}

    # ---- 处理器 -------------------------------------------------------

    def _create(self, params, body):
        return self._service.create_session(
            _require(body, "scenario"),
            str(body.get("actor", "dispatcher")),
            _require(body, "mode"),
            title=str(body.get("title", "")),
            snapshot_digest=str(body.get("snapshot_digest", "")),
            target=body.get("target") if isinstance(body.get("target"), dict) else None,
        )

    def _meta(self, params, body):
        return self._service.registry.get(str(params["sessionID"]))

    def _world(self, params, body):
        return self._service.world_view(str(params["sessionID"]))

    def _timeline(self, params, body):
        return {"timeline": self._service.timeline(
            str(params["sessionID"]), int(params.get("limit", 500)))}

    def _report(self, params, body):
        return self._service.report(str(params["sessionID"]))

    def _prepare(self, params, body):
        return self._service.prepare(str(params["sessionID"]))

    def _start(self, params, body):
        return self._service.start(str(params["sessionID"]),
                                   str(body.get("actor", "dispatcher")))

    def _act(self, params, body):
        action = _require(body, "action")
        given = body.get("params")
        payload = given if isinstance(given, dict) else {
            key: value for key, value in body.items()
            if key not in ("action", "actor")
        }
        return self._service.act(str(params["sessionID"]), action, payload,
                                 str(body.get("actor", "dispatcher")))

    def _tick(self, params, body):
        dt = body.get("dt")
        return self._service.tick(str(params["sessionID"]),
                                  float(dt) if dt is not None else None)

    def _hint(self, params, body):
        return self._service.hint(str(params["sessionID"]),
                                  str(body.get("actor", "dispatcher")))

    def _suspend(self, params, body):
        return self._service.suspend(str(params["sessionID"]),
                                    str(body.get("actor", "dispatcher")),
                                    str(body.get("reason", "")))

    def _resume(self, params, body):
        return self._service.resume(str(params["sessionID"]),
                                    str(body.get("actor", "dispatcher")))

    def _seal(self, params, body):
        return self._service.seal(str(params["sessionID"]),
                                  str(body.get("actor", "dispatcher")))

    def _break_glass(self, params, body):
        return self._service.break_glass(
            str(params["sessionID"]),
            _require(body, "actor"),
            _require(body, "confirmer"),
            str(body.get("reason", "")),
        )

    def _verify(self, params, body):
        return self._service.verify_session(str(params["sessionID"]))
