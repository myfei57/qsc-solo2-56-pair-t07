"""演练领域常量与会话状态机。

会话状态（不可跨越流转，见 :data:`ALLOWED_TRANSITIONS`）：

``PENDING``    会话已登记，还没从现场分叉/装载剧本，不能下发处置动作；
``ARMED``     已上膛：演练平台装载完毕，值班员可开始处置；
``RUNNING``   演练进行中；
``SUSPENDED`` 演练挂起（例如需要独占恢复、或教员冻结时间），挂起期间世界不推进；
``SEALED``    正常结束并封存，历史冻结，只能查看与复盘；
``ABANDONED`` 破玻退出/异常终止，历史同样冻结，会被标记为"非完成"。

只有 ``RUNNING`` 接受处置动作；``SUSPENDED`` 只接受恢复。终态（``SEALED`` /
``ABANDONED``）不可逆——演练一旦结束就不能"再演两秒"，要重来只能新建会话。
"""

from __future__ import annotations

PENDING = "pending"
ARMED = "armed"
RUNNING = "running"
SUSPENDED = "suspended"
SEALED = "sealed"
ABANDONED = "abandoned"

TERMINAL_STATES = frozenset({SEALED, ABANDONED})
ACTIVE_STATES = frozenset({PENDING, ARMED, RUNNING, SUSPENDED})

#: 允许的状态流转。刻意做成白名单：任何没列出的跳转一律拒绝。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({ARMED, ABANDONED}),
    ARMED: frozenset({RUNNING, SUSPENDED, ABANDONED}),
    RUNNING: frozenset({SUSPENDED, SEALED, ABANDONED}),
    SUSPENDED: frozenset({RUNNING, ABANDONED, SEALED}),
    SEALED: frozenset(),
    ABANDONED: frozenset(),
}

#: 演练数据来源。
MODE_SHADOW = "shadow"  # 复制当前实时现场数据后分叉
MODE_REPLAY = "replay"  # 从故障时刻快照重放剧本
DRILL_MODES = frozenset({MODE_SHADOW, MODE_REPLAY})

#: 事件类别。动作类事件会驱动评估；观察类只记录不改变世界。
EVT_ACTION = "action"          # 值班员处置动作（评估对象）
EVT_INJECTION = "injection"    # 教员注入的故障/扰动
EVT_TICK = "tick"              # 世界推进（仿真时钟）
EVT_STATE = "state"            # 会话/演练状态流转
EVT_SYSTEM = "system"          # 出口拦截、恢复、校验等系统事件

ACTION_EVENTS = frozenset({EVT_ACTION, EVT_INJECTION})


def can_transition(current: str, target: str) -> bool:
    """状态机白名单判定。"""

    return target in ALLOWED_TRANSITIONS.get(current, frozenset())
