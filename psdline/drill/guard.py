"""出口门控：演练动作"一条现场动作都不许真发出去"的强制点。

这是演练系统最敏感的组件。演练平台和现场平台跑的是同一套 :class:`~psdline.app.Platform`
代码——如果把演练存储根误指成现场目录、或未来有人接线把演练出口接到真实设备，
必须有人在硬边界上拦下来。这里用多层纵深防御：

1. 物理隔离（首要）：演练平台的存储是快照分叉出来的**独立目录副本**，默认
   根本没有通往现场的写路径；
2. 存储边界（:class:`BoundedStore`）：演练平台持有的所有写操作都被限定在分叉
   目录内，路径越界直接抛 :class:`GuardRejected`；
3. 出口哨兵（:class:`EgressGuard`）：每个对外处置动作在执行前后各过一道
   fail-closed 校验——边界必须 armed、会话必须处于运行态、目标必须在演练域；
4. 现场只读视图（:class:`ReadOnlyLiveView`）：演练读取实时数据时拿到的视图
   没有任何写方法暴露，shadow 采样只能读不能碰。

门控默认拒绝（fail-closed）：状态不明、初始化没完成、边界未 armed，统统拒绝，
绝不"放行试试"。所有拦截尝试都会作为系统事件进演练哈希链（高声量留痕）。
"""

from __future__ import annotations

import os
from typing import Any

from ..errors import StorageError
from ..store import JsonStore
from .errors import GuardRejected

#: 演练域标记。所有演练产物（会话目录、分叉目录、事件）都带这个名字。
DRILL_BOUNDARY = "drill"


class BoundedStore(JsonStore):
    """限定在单一目录根内的存储：任何越界路径一律拒绝。

    包装现场同款 :class:`JsonStore`，原子写语义不变，只多一道边界检查。
    演练平台通过它写分叉副本——即使上层逻辑错误地构造了现场路径，也会在这里
    被挡下，而不是写穿到现场。
    """

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self._real_root = os.path.realpath(self._root)

    def _within_boundary(self, target: str) -> bool:
        real = os.path.realpath(target)
        return real == self._real_root or real.startswith(self._real_root + os.sep)

    def write(self, doc_id: str, payload: Any) -> str:
        target = self.path_of(doc_id)
        if not self._within_boundary(target):
            raise GuardRejected(
                f"演练存储边界拦截：{doc_id} 解析到分叉目录之外（{target}）",
                component="drill-guard",
            )
        return super().write(doc_id, payload)

    def remove(self, doc_id: str) -> bool:
        target = self.path_of(doc_id)
        if not self._within_boundary(target):
            raise GuardRejected(
                f"演练存储边界拦截删除：{doc_id} 越界", component="drill-guard"
            )
        return super().remove(doc_id)


class EgressGuard:
    """处置动作的出口哨兵。

    用法：演练会话开始时 :meth:`arm`（登记允许的分叉目录），每个动作调用
    :meth:`check_egress`；会话终态 :meth:`disarm`。disarm 后或从未 arm 时
    任何检查都失败——默认拒绝。
    """

    def __init__(self) -> None:
        self._armed = False
        self._session_id = ""
        self._fork_roots: set[str] = set()
        self._rejections = 0

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def rejections(self) -> int:
        return self._rejections

    def arm(self, session_id: str, fork_roots: list[str]) -> None:
        """武装边界：登记本会话允许写入的分叉目录。"""

        if self._armed:
            raise GuardRejected("出口边界已 armed，禁止重复武装", component="drill-guard")
        if not fork_roots:
            raise GuardRejected("武装边界必须至少登记一个分叉目录", component="drill-guard")
        self._armed = True
        self._session_id = session_id
        self._fork_roots = {os.path.realpath(path) for path in fork_roots}

    def disarm(self) -> None:
        """解除武装（会话终态）。之后默认拒绝一切。"""

        self._armed = False
        self._session_id = ""
        self._fork_roots.clear()

    def check_egress(self, session_id: str, target_path: str, *, phase: str) -> None:
        """动作执行前后的强制检查；不通过即抛 :class:`GuardRejected`。"""

        if not self._armed:
            self._reject(session_id, target_path, phase, "边界未 armed（fail-closed）")
        if session_id != self._session_id:
            self._reject(session_id, target_path, phase, "会话与边界登记不一致")
        real = os.path.realpath(target_path)
        if not any(real == root or real.startswith(root + os.sep) for root in self._fork_roots):
            self._reject(session_id, target_path, phase, "目标路径不在演练分叉目录内")

    def _reject(self, session_id: str, target_path: str, phase: str, reason: str) -> None:
        self._rejections += 1
        raise GuardRejected(
            f"出口门控拒绝[{phase}] 会话={session_id} 目标={target_path}：{reason}",
            component="drill-guard",
        )


class ReadOnlyLiveView:
    """现场数据的只读视图。

    shadow 模式拿它周期性采样现场实时数据；它只暴露读方法，从类型层面就没有
    写入口——演练侧"看得到现场、动不了现场"。
    """

    def __init__(self, live_store: JsonStore) -> None:
        self._store = live_store

    @property
    def root(self) -> str:
        return self._store.root

    def read(self, doc_id: str) -> Any:
        return self._store.read(doc_id)

    def exists(self, doc_id: str) -> bool:
        return self._store.exists(doc_id)

    def list_ids(self) -> list[str]:
        return self._store.list_ids()


def assert_isolated_fork(fork_root: str, live_root: str) -> None:
    """启动时自检：分叉目录与现场目录必须物理分离、且不是软链接穿透。"""

    fork_real = os.path.realpath(fork_root)
    live_real = os.path.realpath(live_root)
    if fork_real == live_real:
        raise GuardRejected("分叉目录与现场目录相同，演练禁止启动", component="drill-guard")
    if fork_real.startswith(live_real + os.sep) or live_real.startswith(fork_real + os.sep):
        raise GuardRejected("分叉目录与现场目录存在嵌套，演练禁止启动", component="drill-guard")
