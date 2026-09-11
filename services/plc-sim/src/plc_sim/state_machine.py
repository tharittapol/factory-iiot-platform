"""Chamber controller: state machine, actuator logic and safety interlocks.

Like physics.py this knows nothing about Modbus. It receives a decoded command
frame and sensor readings in engineering units, and exposes the resulting state.

Two behaviours here are worth understanding rather than skimming:

* **Fault latching.** Once a fault trips, the controller stays in FAULT until an
  explicit RESET. A fault that clears itself hides the incident from whoever has
  to explain it later.
* **Anti short-cycle.** The heater will not toggle faster than
  ``min_on_off_sec`` even if the temperature says it should. Real heating
  elements and contactors fail early when cycled hard, so this constraint lives
  in the controller, not in a comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import AckStatus, AlarmCode, ChamberState, CommandBit, DesiredMode


@dataclass
class CommandFrame:
    """One decoded write from the gateway."""

    cmd_word: int = 0
    setpoint_c: float = 0.0
    warm_hold_c: float = 0.0
    step_index: int = 0
    profile_len: int = 0
    desired_mode: int = DesiredMode.NONE
    seq: int = 0

    def has(self, bit: CommandBit) -> bool:
        return bool(self.cmd_word & (1 << bit))


@dataclass
class ChamberController:
    params: dict
    setpoint_c: float = 60.0
    warm_hold_c: float = 35.0

    state: ChamberState = ChamberState.READY
    alarm: AlarmCode = AlarmCode.NONE
    ack: AckStatus = AckStatus.NONE
    desired_mode: DesiredMode = DesiredMode.NONE

    step_index: int = 0
    profile_len: int = 0
    last_seq_processed: int = 0
    last_seq_done: int = 0
    state_seq: int = 0

    heater_on: bool = False
    fan_on: bool = False
    fault_latched: bool = False
    gw_link_ok: bool = False

    # Hardware health, flipped by tests or fault injection.
    estop_ok: bool = True
    sensors_ok: list[bool] = field(default_factory=lambda: [True] * 4)
    door_closed: bool = True

    _on_elapsed: float = 0.0
    _off_elapsed: float = 0.0
    _resume_state: ChamberState = ChamberState.RUNNING
    _idle_ready_wait: float = 0.0
    _last_signature: tuple[int, int, int] = (1, 0, 0)

    def __post_init__(self) -> None:
        control = self.params.get("control", {})
        safety = self.params.get("safety", {})
        self.hysteresis_c: float = float(control.get("hysteresis_x10", 10)) / 10.0
        self.min_on_off_sec: float = float(control.get("min_on_off_sec", 30))
        self.overtemp_trip_c: float = float(safety.get("overtemp_trip_x10", 950)) / 10.0
        self.idle_ready_delay: float = 0.5

    # -- commands -----------------------------------------------------------

    def apply_command(self, frame: CommandFrame) -> None:
        """Handle a command frame. Only called when cmd_seq changed."""
        self.last_seq_done = frame.seq
        self.last_seq_processed = frame.seq
        self.ack = AckStatus.ACCEPTED
        self.desired_mode = DesiredMode(frame.desired_mode)

        if self.gw_link_ok:
            self.setpoint_c = frame.setpoint_c
            self.warm_hold_c = frame.warm_hold_c
            self.step_index = frame.step_index
            self.profile_len = frame.profile_len

        recognised = self._dispatch(frame)
        if not recognised and frame.cmd_word != 0:
            self.ack = AckStatus.REJECTED

    def _dispatch(self, frame: CommandFrame) -> bool:
        if frame.has(CommandBit.RESET):
            if self.state in (ChamberState.FAULT, ChamberState.STOPPED):
                self._clear_faults()
                self._to_idle()
            return True

        if frame.has(CommandBit.STOP):
            if self.state in (
                ChamberState.RUNNING,
                ChamberState.WARM_HOLD,
                ChamberState.WAITING,
                ChamberState.PAUSED,
            ):
                self.state = ChamberState.STOPPED
                self._idle_ready_wait = self.idle_ready_delay
            return True

        if not self.gw_link_ok:
            # Without a live gateway link the chamber accepts only STOP/RESET.
            return False

        if self.state == ChamberState.READY:
            if frame.has(CommandBit.WAIT):
                self.state = ChamberState.WAITING
                return True
            if frame.has(CommandBit.START):
                self.state = ChamberState.RUNNING
                return True

        elif self.state == ChamberState.WAITING:
            if frame.has(CommandBit.START):
                self.state = ChamberState.RUNNING
                return True
            if frame.has(CommandBit.PAUSE):
                self._pause(ChamberState.WAITING)
                return True

        elif self.state == ChamberState.RUNNING:
            if frame.has(CommandBit.WARM):
                self.state = ChamberState.WARM_HOLD
                return True
            if frame.has(CommandBit.PAUSE):
                self._pause(ChamberState.RUNNING)
                return True
            if frame.has(CommandBit.SET):
                return True

        elif self.state == ChamberState.WARM_HOLD:
            if frame.has(CommandBit.PAUSE):
                self._pause(ChamberState.WARM_HOLD)
                return True
            if frame.has(CommandBit.SET):
                return True

        elif self.state == ChamberState.PAUSED:
            if frame.has(CommandBit.RESUME):
                self.state = self._resume_state
                return True

        return False

    def _pause(self, from_state: ChamberState) -> None:
        self._resume_state = from_state
        self.state = ChamberState.PAUSED

    def _to_idle(self) -> None:
        self.state = ChamberState.IDLE
        self._idle_ready_wait = self.idle_ready_delay

    def _clear_faults(self) -> None:
        self.fault_latched = False
        self.alarm = AlarmCode.NONE
        self._on_elapsed = 0.0
        self._off_elapsed = 0.0

    # -- per-tick -----------------------------------------------------------

    def step(self, dt: float, temps_c: list[float]) -> None:
        """Advance one tick. ``temps_c`` are engineering-unit readings."""
        self._advance_state(dt)
        self._update_actuators(dt, temps_c)
        self._check_safety(temps_c)
        self._update_ack()
        self._bump_state_seq()

    def _advance_state(self, dt: float) -> None:
        if self.fault_latched:
            self.state = ChamberState.FAULT
            return

        if self.state == ChamberState.STOPPED:
            self._idle_ready_wait -= dt
            if self._idle_ready_wait <= 0:
                self._to_idle()
        elif self.state == ChamberState.IDLE:
            self._idle_ready_wait -= dt
            if self._idle_ready_wait <= 0:
                self.state = ChamberState.READY

    @property
    def target_c(self) -> float:
        if self.state == ChamberState.RUNNING:
            return self.setpoint_c
        if self.state == ChamberState.WARM_HOLD:
            return self.warm_hold_c
        return 0.0

    def _update_actuators(self, dt: float, temps_c: list[float]) -> None:
        self.fan_on = not self.fault_latched and self.state in (
            ChamberState.RUNNING,
            ChamberState.WARM_HOLD,
            ChamberState.WAITING,
        )

        allow_heat = (
            not self.fault_latched
            and self.state in (ChamberState.RUNNING, ChamberState.WARM_HOLD)
            and self.target_c > 0
        )
        if not allow_heat:
            self.heater_on = False
            self._on_elapsed = 0.0
            self._off_elapsed = 0.0
            return

        temp_max = max(temps_c) if temps_c else 0.0
        lower = self.target_c - self.hysteresis_c
        upper = self.target_c + self.hysteresis_c

        if temp_max < lower:
            self._on_elapsed += dt
            self._off_elapsed = 0.0
        elif temp_max > upper:
            self._off_elapsed += dt
            self._on_elapsed = 0.0
        else:
            self._on_elapsed = 0.0
            self._off_elapsed = 0.0

        # Demand must persist for min_on_off_sec before the heater flips.
        if self._on_elapsed >= self.min_on_off_sec and not self.heater_on:
            self.heater_on = True
            self._on_elapsed = 0.0
        elif self._off_elapsed >= self.min_on_off_sec and self.heater_on:
            self.heater_on = False
            self._off_elapsed = 0.0

    def _check_safety(self, temps_c: list[float]) -> None:
        """Latching trips first, then non-latching warnings.

        Losing the gateway raises an alarm but does not stop the chamber. A
        drying batch that is interrupted mid-cycle is scrap, so a brief network
        outage must not destroy the load; an unattended chamber still has to be
        visible, so it announces itself instead of running silently. Commands
        other than STOP and RESET stay refused while the link is down.
        """
        temp_max = max(temps_c) if temps_c else 0.0

        if not self.estop_ok:
            self._trip(AlarmCode.E_STOP)
            return
        if temp_max >= self.overtemp_trip_c:
            self._trip(AlarmCode.OVERTEMP)
            return
        if not all(self.sensors_ok):
            self._trip(AlarmCode.SENSOR_FAIL)
            return
        if self.fault_latched:
            return  # keep reporting the latched fault until RESET

        self.alarm = AlarmCode.NONE if self.gw_link_ok else AlarmCode.GW_LINK_LOST

    def _trip(self, alarm: AlarmCode) -> None:
        self.alarm = alarm
        self.fault_latched = True
        self.state = ChamberState.FAULT
        self.heater_on = False
        self.fan_on = False

    def _update_ack(self) -> None:
        if self.fault_latched:
            self.ack = AckStatus.FAILED
        elif self.state in (ChamberState.RUNNING, ChamberState.WARM_HOLD):
            self.ack = AckStatus.EXECUTING
        elif self.state in (ChamberState.IDLE, ChamberState.STOPPED, ChamberState.PAUSED):
            self.ack = AckStatus.COMPLETED
        elif self.state == ChamberState.WAITING:
            self.ack = AckStatus.ACCEPTED

    def _bump_state_seq(self) -> None:
        signature = (int(self.state), int(self.alarm), self.step_index)
        if signature != self._last_signature:
            self.state_seq += 1
            self._last_signature = signature

    # -- reporting ----------------------------------------------------------

    @property
    def alarm_active(self) -> bool:
        return self.alarm != AlarmCode.NONE
