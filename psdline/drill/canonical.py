"""演练专用的规范化序列化与指纹。

演练要做到「同一份快照 + 同一条事件流，重放出来的现场状态逐字节一致」，前提是
序列化结果与字典插入顺序、文件遍历顺序、平台差异都无关。这里统一三件事：

* 键排序：``dict`` 一律按键名字典序输出；
* 标量收敛：``None`` 保留、布尔/整数/字符串原样、浮点用 ``repr`` 最短往返；
* 指纹：对规范化后的 UTF-8 字节取 SHA-256。

指纹只用于「演练沙箱当前态」的内容寻址与重放校验，绝不覆盖现场存储。
"""

from __future__ import annotations

import hashlib
from typing import Any


def canonicalize(value: Any) -> str:
    """把任意 JSON 兼容值转成确定性的规范化字符串。"""

    return _encode(value)


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 最短往返表示，保证 1.0 / 1.10 这类输入在不同机器上一致。
        return repr(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            _json_string(str(key)) + ":" + _encode(value[key])
            for key in sorted(value, key=lambda item: str(item))
        ]
        return "{" + ",".join(parts) + "}"
    # 落到这里说明演练状态里混进了非 JSON 类型，应当在演练边界就暴露。
    raise TypeError(f"演练状态出现不可规范化的类型: {type(value).__name__}")


def _json_string(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def digest(value: Any) -> str:
    """返回规范化值的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(canonicalize(value).encode("utf-8")).hexdigest()


def short(value: Any, length: int = 12) -> str:
    """返回短摘要，用于演练编号等人类可读标识。"""

    return digest(value)[:length]
