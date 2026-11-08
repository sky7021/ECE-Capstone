"""LED state badges (S0-S5, S2.A2/S2.B2 orthogonal locks, ZOH health)."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from software.fsm.host_fsm import HostState


class StatusIndicator(QWidget):
    """Displays the active Host FSM state, S2 orthogonal locks, and ZOH cache health."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.current_state = HostState.S0_DISCONNECTED
        self.s2_a2_locked = False
        self.s2_b2_locked = False
        self.zoh_healthy = False

        layout = QHBoxLayout(self)
        self.state_label = QLabel(self.current_state.label, self)
        self.s2_a2_badge = QLabel("S2.A2: UNLOCKED", self)
        self.s2_b2_badge = QLabel("S2.B2: UNLOCKED", self)
        self.zoh_badge = QLabel("ZOH: STALE", self)
        for w in (self.state_label, self.s2_a2_badge, self.s2_b2_badge, self.zoh_badge):
            layout.addWidget(w)

    def set_state(self, state: HostState) -> None:
        self.current_state = state
        self.state_label.setText(state.label)
        if state is HostState.S3_ACTIVE_STREAMING:
            self.set_s2_locks(True, True)
            self.set_zoh_health(True)
        elif state in (HostState.S0_DISCONNECTED, HostState.S1_IDLE_STANDBY, HostState.S4_FAULT):
            self.set_s2_locks(False, False)
            self.set_zoh_health(False)

    def set_s2_locks(self, in_s2_a2: bool, in_s2_b2: bool) -> None:
        self.s2_a2_locked = bool(in_s2_a2)
        self.s2_b2_locked = bool(in_s2_b2)
        self.s2_a2_badge.setText(f"S2.A2: {'LOCKED' if self.s2_a2_locked else 'WAIT'}")
        self.s2_b2_badge.setText(f"S2.B2: {'LOCKED' if self.s2_b2_locked else 'WAIT'}")

    def set_zoh_health(self, healthy: bool) -> None:
        self.zoh_healthy = bool(healthy)
        self.zoh_badge.setText(f"ZOH: {'FRESH' if self.zoh_healthy else 'STALE'}")

