"""跨进程文件锁（标准库 fcntl）。

并发演练允许多个值班员同时开多个会话；它们目录不同、互不强锁。但同一个会话可能
被两个 CLI 进程同时操作（例如一个在推进、另一条命令恰好重入），需要一把会话级
咨询锁，把“读流水—判定—追加—更新检查点”串行化。锁随进程退出由内核释放，因此
持锁进程崩溃不会留下永久锁。
"""

from __future__ import annotations

import contextlib
import fcntl
import os


@contextlib.contextmanager
def file_lock(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
