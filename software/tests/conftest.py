"""Shared pytest fixtures: a process-wide QCoreApplication and an event-pumping wait helper."""

from __future__ import annotations

import os
import time
from typing import Callable

import software.tests  # noqa: F401 - activates pytest shim when pytest is not installed
import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> QCoreApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def pump(app: QCoreApplication, seconds: float) -> None:
    """Process Qt events (delivering queued cross-thread signals) for ``seconds``."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(app: QCoreApplication, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


@pytest.fixture
def waiter(qapp):
    def _wait(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        return wait_until(qapp, predicate, timeout)

    _wait.pump = lambda s: pump(qapp, s)  # type: ignore[attr-defined]
    return _wait

