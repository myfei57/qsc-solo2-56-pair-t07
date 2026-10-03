"""规范化序列化：与插入顺序无关、指纹稳定。"""

from __future__ import annotations

from psdline.drill.canonical import canonicalize, digest


def test_dict_order_independent():
    a = {"a": 1, "b": [1, 2, 3], "c": {"x": True, "y": None}}
    b = {"c": {"y": None, "x": True}, "b": [1, 2, 3], "a": 1}
    assert canonicalize(a) == canonicalize(b)
    assert digest(a) == digest(b)


def test_list_order_significant():
    assert canonicalize([1, 2, 3]) != canonicalize([3, 2, 1])


def test_stable_across_types():
    assert canonicalize(True) == "true"
    assert canonicalize(False) == "false"
    assert canonicalize(None) == "null"
    assert canonicalize(1) == "1"
    assert canonicalize("a") == '"a"'


def test_rejects_opaque_types():
    import pytest

    with pytest.raises(TypeError):
        canonicalize({"x": object()})
