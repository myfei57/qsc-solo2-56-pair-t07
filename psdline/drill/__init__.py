"""应急演练子系统。

在不动任何现场设备的前提下，用真实站台与列车数据推演应急处置。四件事一体保证：

* **演练**：从现场只读快照或实时分叉出发，在确定性沙箱里跑真实业务调用链；
* **门控**：fail-closed 的 :class:`~psdline.drill.gate.CommandGate`，演练指令
  永不触达现场出口；
* **审计**：与现场审计物理分离的哈希链演练审计，封存后不可变、可独立验签；
* **存储**：内容寻址不可变快照 + 每会话只追加哈希链事件流 + 可重建检查点。

支持并发演练（每会话独立目录/锁/令牌）、部分失败恢复（事件先行，重放重建）、
崩溃安全（原子写 + fsync + 撕裂尾裁剪）与历史不可变（无删除接口、哈希链、HMAC
封存）。
"""

from __future__ import annotations

from .snapshot import SnapshotStore
from .service import DrillService
from .scenario import SCENARIOS, get_scenario

__all__ = ["DrillService", "SnapshotStore", "SCENARIOS", "get_scenario"]
