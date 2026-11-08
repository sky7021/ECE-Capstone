"""Lightweight fallback for ``pytest`` primitives when running in an offline env without pytest.

When real ``pytest`` is installed, this module is never activated. When ``pytest``
is absent, :func:`install` registers this module as ``sys.modules["pytest"]`` and
provides ``approx``, ``raises``, ``fixture``, ``mark.parametrize``, and a
``unittest.TestSuite`` adapter so the full test suite can run via
``python -m unittest discover -s software/tests`` or ``python -m software.tests.test_pipeline``.
"""

from __future__ import annotations

import inspect
import math
import re
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable


class _Approx:
    def __init__(self, expected: float, rel: float = 1e-6, abs_tol: float = 1e-12) -> None:
        self.expected = float(expected)
        self.rel = rel
        self.abs_tol = abs_tol

    def __eq__(self, actual: object) -> bool:
        if not isinstance(actual, (int, float)):
            return False
        return math.isclose(float(actual), self.expected, rel_tol=self.rel, abs_tol=self.abs_tol)

    def __repr__(self) -> str:
        return f"approx({self.expected!r}, rel={self.rel}, abs={self.abs_tol})"


def approx(expected: float, rel: float = 1e-6, abs: float = 1e-12) -> _Approx:  # noqa: A002
    return _Approx(expected, rel=rel, abs_tol=abs)


@contextmanager
def raises(exc_type: type[BaseException] | tuple[type[BaseException], ...], match: str | None = None):
    try:
        yield
    except exc_type as exc:
        if match is not None and not re.search(match, str(exc)):
            raise AssertionError(f"{exc!r} did not match {match!r}") from exc
        return
    raise AssertionError(f"Expected {exc_type} to be raised, but no exception was raised")


def fixture(fn: Callable | None = None, *, scope: str = "function"):
    def _wrap(f: Callable) -> Callable:
        f._is_fixture = True  # type: ignore[attr-defined]
        f._fixture_scope = scope  # type: ignore[attr-defined]
        return f

    return _wrap(fn) if fn is not None else _wrap


class _Mark:
    @staticmethod
    def parametrize(argnames: str | Iterable[str], argvalues: Iterable[Any]):
        names = [a.strip() for a in argnames.split(",")] if isinstance(argnames, str) else list(argnames)
        vals = list(argvalues)

        def _decorator(fn: Callable) -> Callable:
            existing = list(getattr(fn, "_parametrize", []))
            existing.append((names, vals))
            fn._parametrize = existing  # type: ignore[attr-defined]
            return fn

        return _decorator


mark = _Mark()


def _collect_fixtures(module: types.ModuleType) -> dict[str, Callable]:
    fixtures: dict[str, Callable] = {}
    try:
        from software.tests import conftest

        for name, obj in inspect.getmembers(conftest, callable):
            if getattr(obj, "_is_fixture", False):
                fixtures[name] = obj
    except Exception:
        pass
    for name, obj in inspect.getmembers(module, callable):
        if getattr(obj, "_is_fixture", False):
            fixtures[name] = obj
    return fixtures


def build_unittest_suite(module: types.ModuleType) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    fixtures = _collect_fixtures(module)
    session_cache: dict[str, Any] = {}

    def resolve_fixture(name: str, cache: dict[str, Any], cleanups: list[Callable[[], None]]) -> Any:
        if name in cache:
            return cache[name]
        if name == "tmp_path":
            td = tempfile.TemporaryDirectory()
            cleanups.append(td.cleanup)
            p = Path(td.name)
            cache[name] = p
            return p
        fn = fixtures[name]
        scope = getattr(fn, "_fixture_scope", "function")
        if scope == "session" and name in session_cache:
            return session_cache[name]
        sig = inspect.signature(fn)
        kwargs = {p: resolve_fixture(p, cache, cleanups) for p in sig.parameters}
        res = fn(**kwargs)
        if inspect.isgenerator(res):
            val = next(res)
            if scope != "session":
                cleanups.append(lambda g=res: next(g, None))
        else:
            val = res
        if scope == "session":
            session_cache[name] = val
        cache[name] = val
        return val

    def make_case(fn: Callable, instance_factory: Callable[[], Any] | None, label: str, extra_args: dict[str, Any]):
        class _FunctionTestCase(unittest.TestCase):
            def shortDescription(self) -> str:
                return label

            def runTest(self) -> None:
                cache: dict[str, Any] = dict(extra_args)
                cleanups: list[Callable[[], None]] = []
                try:
                    sig = inspect.signature(fn)
                    call_kwargs: dict[str, Any] = {}
                    for pname in sig.parameters:
                        if pname == "self":
                            continue
                        if pname in cache:
                            call_kwargs[pname] = cache[pname]
                        elif pname in fixtures or pname == "tmp_path":
                            call_kwargs[pname] = resolve_fixture(pname, cache, cleanups)
                    target = instance_factory() if instance_factory is not None else None
                    if target is not None:
                        getattr(target, fn.__name__)(**call_kwargs)
                    else:
                        fn(**call_kwargs)
                finally:
                    for c in reversed(cleanups):
                        try:
                            c()
                        except Exception:
                            pass

        _FunctionTestCase.__name__ = label
        _FunctionTestCase.__qualname__ = label
        return _FunctionTestCase()

    def expand_and_add(fn: Callable, instance_factory: Callable[[], Any] | None, prefix: str) -> None:
        param_specs = getattr(fn, "_parametrize", [])
        if not param_specs:
            suite.addTest(make_case(fn, instance_factory, prefix, {}))
            return
        names, values = param_specs[0]
        for idx, row in enumerate(values):
            if len(names) == 1:
                extra = {names[0]: row}
            else:
                extra = dict(zip(names, row))
            suite.addTest(make_case(fn, instance_factory, f"{prefix}[{idx}]", extra))

    for name, obj in inspect.getmembers(module):
        if inspect.isFunction(obj) if hasattr(inspect, "isFunction") else inspect.isfunction(obj):
            if name.startswith("test_"):
                expand_and_add(obj, None, f"{module.__name__}.{name}")
        elif inspect.isclass(obj) and name.startswith("Test") and obj.__module__ == module.__name__:
            for mname, mobj in inspect.getmembers(obj, inspect.isfunction):
                if mname.startswith("test_"):
                    expand_and_add(mobj, obj, f"{module.__name__}.{name}.{mname}")
    return suite


def run_module(module: types.ModuleType) -> int:
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(build_unittest_suite(module))
    return 0 if result.wasSuccessful() else 1


def install() -> None:
    if "pytest" not in sys.modules:
        sys.modules["pytest"] = sys.modules[__name__]

