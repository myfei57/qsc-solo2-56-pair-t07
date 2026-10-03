"""跨进程崩溃演示的"值班进程"工作脚本（配合 tools/drill_crash_demo.py）。

由演示器以子进程方式启动。它打开一个已在跑的演练会话，下发处置动作；收到
``CRASH`` 指令后用 ``os._exit(9)`` 立即死亡——不执行任何 finally/atexit/缓冲
刷新，模拟进程在动作执行瞬间被 kill -9/掉电。父进程随后用一个**全新**服务
实例做恢复，验证：

- request 意图已 fsync 落盘、result 未落盘时被识别为 in_flight；
- 会话进入 SUSPENDED 而非悄悄继续；
- 恢复绝不自动重发动作；
- 值班员显式 resume 后能继续，封存后深度复核仍然通过。

用法：
    python -m tools.crash_worker <drill_root> <session_id> <phase>
phase = act | crash
"""

from __future__ import annotations

import os
import sys

# 允许以脚本方式直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psdline.app import build_platform  # noqa: E402
from psdline.clock import ManualClock  # noqa: E402
from psdline.config import default_config  # noqa: E402
from psdline.drill import DrillService  # noqa: E402


def main() -> int:
    drill_root, session_id, phase = sys.argv[1], sys.argv[2], sys.argv[3]
    # 现场只作为句柄占位（恢复路径不触碰现场写）；用独立临时目录，绝不与
    # 父进程的现场/演练目录共享。
    import tempfile

    live = build_platform(default_config(), clock=ManualClock(),
                          storage_root=tempfile.mkdtemp(prefix="crash-live-"))
    service = DrillService(drill_root, live, shadow_speed=0.0)

    if phase == "act":
        # 正常动作：清扫 + 确认，证明子进程能真的操作会话
        service.act(session_id, "sweep", {"station_id": "ST1"}, "crash-worker")
        service.act(session_id, "fault_status", {"psd_id": "ST1-G01-01"}, "crash-worker")
        print("ACTED", flush=True)
        return 0

    if phase == "crash":
        # 手动制造"意图已落、结果未落"：在会话锁内写 request 后立即硬死亡。
        from psdline.drill.registry import locked_session

        with locked_session(service.registry, session_id):
            runtime = service._require_runtime(session_id)
            runtime.log.append("action", {
                "phase": "request",
                "request_id": "req-killed",
                "action": "clear_fault",
                "params": {"psd_id": "ST1-G01-01"},
                "actor": "crash-worker",
            }, actor="crash-worker")
            # 不写 result，直接硬退出（fsync 已在 append 内完成）
            os._exit(9)

    print("unknown phase", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
