"""演练领域的错误类型。

演练错误与现场 :class:`~psdline.errors.RailError` 刻意分开：演练痕迹、会话状态、
门控拒绝是演练域自己的事，不允许被现场错误处理器吞掉后误写成现场审计。
"""

from __future__ import annotations


class DrillError(Exception):
    """演练子系统异常基类，携带组件名。"""

    def __init__(self, message: str, *, component: str = "drill") -> None:
        super().__init__(message)
        self.message = message
        self.component = component

    def as_record(self) -> dict:
        return {
            "type": type(self).__name__,
            "component": self.component,
            "message": self.message,
        }


class SessionStateError(DrillError):
    """会话当前状态不允许该操作（例如已封存还想继续演）。"""


class SessionNotFoundError(DrillError):
    """会话标识不存在。"""


class GuardRejected(DrillError):
    """出口门控拒绝了一个试图离开演练边界的动作。

    这是整个演练系统最严重的一类异常：正常流程里永远不该发生，一旦发生说明
    接线错误或有人试图绕过隔离，必须 fail-closed 并高声量告警。
    """


class EvaluateError(DrillError):
    """剧本或处置动作无法被评估（参数缺失、引用了不存在的实体）。"""


class LogIntegrityError(DrillError):
    """哈希链或快照指纹校验失败，演练历史可能被篡改。"""
