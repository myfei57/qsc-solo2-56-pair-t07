#!/usr/bin/env python3
"""零依赖测试运行器（pytest 离线兜底）。

环境无 pytest 又无法联网时，用它执行 tests/ 下全部 ``test_`` 函数。真正的 CI
请用 ``make test``（pytest）。本运行器支持：

- ``tmp_path``（临时目录）、``clock``（ManualClock）、``platform``（干净分叉
  平台）、``online``（已到站开门的平台）四个夹具，与 tests/conftest.py 对齐；
- ``pytest.raises``（通过内联垫片模块提供）；
- 测试文件里的 ``import pytest`` 会被 import 钩子导向垫片。

用法：``python tools/run_tests.py``
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))


# ---- pytest 垫片 ------------------------------------------------------

class _Raises:
    def __init__(self, expected):
        self.expected = expected
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"预期 {self.expected.__name__} 未被抛出")
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        return True


class _Marks:
    def __getattr__(self, name):
        def apply(obj=None, *a, **k):
            return obj
        return apply


def _fixture(*args, **kwargs):
    def deco(func):
        func._is_fixture = True
        return func
    if args and callable(args[0]):
        return deco(args[0])
    return deco


_pytest = types.ModuleType("pytest")
_pytest.raises = _Raises
_pytest.fixture = _fixture
_pytest.mark = _Marks()
sys.modules["pytest"] = _pytest


# ---- 夹具（与 conftest.py 语义对齐） ----------------------------------

from psdline.app import build_platform  # noqa: E402
from psdline.clock import ManualClock  # noqa: E402
from psdline.config import default_config  # noqa: E402


def make_clock():
    return ManualClock()


def make_platform():
    path = Path(tempfile.mkdtemp(prefix="run-tests-")) / "state"
    return build_platform(default_config(), clock=ManualClock(),
                          storage_root=str(path))


def make_online():
    platform = make_platform()
    platform.start()
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    return platform


def make_tmp_path():
    return Path(tempfile.mkdtemp(prefix="run-tests-"))


FIXTURES = {
    "clock": make_clock,
    "platform": make_platform,
    "online": make_online,
    "tmp_path": make_tmp_path,
}


def load_module(path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def run_file(path: Path) -> tuple[int, int, list[str]]:
    module = load_module(path)
    # 收集模块内用 @pytest.fixture 定义的夹具（垫片给函数盖了 _is_fixture 标记）
    local_fixtures = {
        name: obj for name, obj in list(vars(module).items())
        if getattr(obj, "_is_fixture", False)
    }

    def build(name):
        if name in FIXTURES:
            return FIXTURES[name]()
        func = local_fixtures.get(name)
        if func is None:
            raise KeyError(name)
        kwargs = {}
        for param in inspect.signature(func).parameters:
            kwargs[param] = build(param)
        produced = func(**kwargs)
        # 支持 yield 夹具：返回产出值，teardown 注册在用例后执行
        if inspect.isgenerator(produced):
            value = next(produced)
            finalizers.append(lambda gen=produced: (next(gen, None) or None))
            return value
        return produced

    passed = failed = 0
    failures = []
    for name, func in list(vars(module).items()):
        if not name.startswith("test_") or not callable(func):
            continue
        kwargs = {}
        finalizers = []
        try:
            for param in inspect.signature(func).parameters:
                kwargs[param] = build(param)
        except KeyError:
            failures.append(f"{path.name}::{name}\n未知夹具: {param}")
            failed += 1
            continue
        except Exception:  # noqa: BLE001
            failures.append(f"{path.name}::{name}\n夹具构造失败:\n{traceback.format_exc()}")
            failed += 1
            continue
        try:
            func(**kwargs)
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            failures.append(f"{path.name}::{name}\n{traceback.format_exc()}")
        finally:
            for finalize in reversed(finalizers):
                try:
                    finalize()
                except StopIteration:
                    pass
                except Exception:  # noqa: BLE001
                    pass
    return passed, failed, failures


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    files = [Path(arg) for arg in args] if args else sorted(TESTS.glob("test_*.py"))
    total_pass = total_fail = 0
    all_failures = []
    for path in files:
        p, f, failures = run_file(path)
        total_pass += p
        total_fail += f
        all_failures.extend(failures)
        status = "ok" if not f else f"{f} 失败"
        print(f"[{'PASS' if not f else 'FAIL'}] {path.name}: {p} 通过, {status}")
    if all_failures:
        print("\n" + "=" * 70 + "\n失败详情:\n")
        print("\n".join(all_failures))
    print(f"\n合计 {total_pass} 通过, {total_fail} 失败")
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
