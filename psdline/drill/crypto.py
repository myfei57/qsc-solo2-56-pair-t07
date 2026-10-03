"""规范化序列化与哈希。

演练痕迹要做到历史不可变，所有落盘的事件、快照、封存清单都必须有一个稳定的
内容指纹。Python 的 ``json.dumps`` 默认不保证键序、且对非 ASCII 字符转义，这里
统一成：键按字典序、无空白、``ensure_ascii=False``、分隔符固定。同样的内容
无论从哪个进程、哪个 Python 版本来，算出的 SHA-256 都一致——这是哈希链可被
独立复核的前提。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_dumps(payload: Any) -> str:
    """把任意 JSON 可序列化对象转成规范化文本。

    排序键、紧凑分隔、不转义中文；``sort_keys`` 对嵌套对象递归生效。
    """

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def content_hash(payload: Any) -> str:
    """计算一个对象的 SHA-256 内容指纹（64 位十六进制小写）。"""

    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


def chain_hash(previous_hash: str, payload: Any) -> str:
    """把"上一条指纹 + 本条内容"接起来再哈希，形成哈希链一环。

    任何一条被改、被删或顺序被调换，从该条起后续指纹全部对不上。
    """

    linked = {"prev": previous_hash, "body": payload}
    return content_hash(linked)
