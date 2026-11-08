"""Hardware-decoupled emulation & verification suite."""

try:
    import pytest as _pytest  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - fallback when pytest is not installed
    from software.tests import _pytest_shim

    _pytest_shim.install()
