"""应急演练子系统。

在真实站台/列车数据上推演应急处置，但任何现场动作都不真正外发。核心四件套：

- 演练：:class:`~psdline.drill.service.DrillService` + 剧本（shadow 实时分叉 /
  replay 快照重放）；
- 门控：:mod:`psdline.drill.guard`（物理隔离 + 边界存储 + fail-closed 出口）；
- 审计：:mod:`psdline.drill.log`（哈希链 WORM）与现场审计完全分离；
- 存储：:mod:`psdline.drill.fork` / :mod:`psdline.drill.registry`（会话独立目录、
  内容寻址不可变快照）。
"""

from __future__ import annotations

from .errors import (
    DrillError,
    EvaluateError,
    GuardRejected,
    LogIntegrityError,
    SessionNotFoundError,
    SessionStateError,
)
from .model import (
    ABANDONED,
    ARMED,
    MODE_REPLAY,
    MODE_SHADOW,
    PENDING,
    RUNNING,
    SEALED,
    SUSPENDED,
)
from .scenarios import SCENARIOS, scenario_catalog
from .service import DrillService

__all__ = [
    "DrillService",
    "SCENARIOS",
    "scenario_catalog",
    "MODE_SHADOW",
    "MODE_REPLAY",
    "PENDING",
    "ARMED",
    "RUNNING",
    "SUSPENDED",
    "SEALED",
    "ABANDONED",
    "DrillError",
    "SessionStateError",
    "SessionNotFoundError",
    "GuardRejected",
    "LogIntegrityError",
    "EvaluateError",
]
