"""Tests for the chamber state machine and safety interlocks."""

from __future__ import annotations

import pytest

from plc_sim.constants import AckStatus, AlarmCode, ChamberState, CommandBit
from plc_sim.state_machine import ChamberController, CommandFrame


def frame(*bits, seq=1, setpoint=65.0, warm=35.0, **kw):
    word = 0
    for b in bits:
        word |= 1 << b
    return CommandFrame(cmd_word=word, seq=seq, setpoint_c=setpoint,
                        warm_hold_c=warm, **kw)


@pytest.fixture
def ctl(cfg):
    c = ChamberController(params=cfg.simulation)
    c.gw_link_ok = True
    return c


def test_starts_ready(ctl):
    assert ctl.state is ChamberState.READY


def test_start_moves_to_running(ctl):
    ctl.apply_command(frame(CommandBit.START))
    assert ctl.state is ChamberState.RUNNING
    assert ctl.setpoint_c == 65.0


def test_stop_from_running(ctl):
    ctl.apply_command(frame(CommandBit.START, seq=1))
    ctl.apply_command(frame(CommandBit.STOP, seq=2))
    assert ctl.state is ChamberState.STOPPED


def test_pause_and_resume_returns_to_previous_phase(ctl):
    ctl.apply_command(frame(CommandBit.START, seq=1))
    ctl.apply_command(frame(CommandBit.PAUSE, seq=2))
    assert ctl.state is ChamberState.PAUSED
    ctl.apply_command(frame(CommandBit.RESUME, seq=3))
    assert ctl.state is ChamberState.RUNNING


def test_command_without_gateway_link_is_rejected(cfg):
    c = ChamberController(params=cfg.simulation)
    c.gw_link_ok = False
    c.apply_command(frame(CommandBit.START))
    assert c.state is ChamberState.READY
    assert c.ack is AckStatus.REJECTED


def test_stop_works_even_without_link(cfg):
    c = ChamberController(params=cfg.simulation)
    c.gw_link_ok = True
    c.apply_command(frame(CommandBit.START, seq=1))
    c.gw_link_ok = False
    c.apply_command(frame(CommandBit.STOP, seq=2))
    assert c.state is ChamberState.STOPPED


def test_repeated_seq_is_ignored_by_caller_contract(ctl):
    """apply_command records the seq so main.py can skip duplicates."""
    ctl.apply_command(frame(CommandBit.START, seq=7))
    assert ctl.last_seq_processed == 7
    assert ctl.last_seq_done == 7


def test_overtemp_trips_and_latches(ctl):
    ctl.apply_command(frame(CommandBit.START))
    ctl.step(1.0, [96.0, 60.0, 60.0, 60.0])
    assert ctl.state is ChamberState.FAULT
    assert ctl.alarm is AlarmCode.OVERTEMP

    # Temperature back to normal must NOT clear the fault on its own.
    ctl.step(1.0, [60.0, 60.0, 60.0, 60.0])
    assert ctl.state is ChamberState.FAULT
    assert ctl.fault_latched


def test_reset_clears_fault(ctl):
    ctl.apply_command(frame(CommandBit.START, seq=1))
    ctl.step(1.0, [96.0] * 4)
    ctl.apply_command(frame(CommandBit.RESET, seq=2))
    assert ctl.alarm is AlarmCode.NONE
    assert ctl.state is ChamberState.IDLE
    # After the idle delay it becomes READY again.
    ctl.step(1.0, [60.0] * 4)
    assert ctl.state is ChamberState.READY


def test_estop_trips(ctl):
    ctl.apply_command(frame(CommandBit.START))
    ctl.estop_ok = False
    ctl.step(1.0, [60.0] * 4)
    assert ctl.alarm is AlarmCode.E_STOP
    assert ctl.heater_on is False


def test_sensor_failure_trips(ctl):
    ctl.apply_command(frame(CommandBit.START))
    ctl.sensors_ok[2] = False
    ctl.step(1.0, [60.0] * 4)
    assert ctl.alarm is AlarmCode.SENSOR_FAIL


def test_heater_respects_min_on_off(ctl):
    """Demand alone must not flip the heater until it persists."""
    ctl.apply_command(frame(CommandBit.START))
    cold = [40.0] * 4

    ctl.step(1.0, cold)
    assert ctl.heater_on is False, "should not fire on the first cold scan"

    for _ in range(int(ctl.min_on_off_sec) + 1):
        ctl.step(1.0, cold)
    assert ctl.heater_on is True


def test_heater_off_within_hysteresis_band(ctl):
    ctl.apply_command(frame(CommandBit.START))
    for _ in range(int(ctl.min_on_off_sec) + 2):
        ctl.step(1.0, [40.0] * 4)
    assert ctl.heater_on is True

    # Inside the band the timers reset, so the heater stays put.
    for _ in range(int(ctl.min_on_off_sec) + 2):
        ctl.step(1.0, [65.0] * 4)
    assert ctl.heater_on is True

    for _ in range(int(ctl.min_on_off_sec) + 2):
        ctl.step(1.0, [80.0] * 4)
    assert ctl.heater_on is False


def test_state_seq_increments_only_on_change(ctl):
    ctl.step(1.0, [40.0] * 4)
    before = ctl.state_seq
    ctl.step(1.0, [40.0] * 4)
    assert ctl.state_seq == before

    ctl.apply_command(frame(CommandBit.START))
    ctl.step(1.0, [40.0] * 4)
    assert ctl.state_seq == before + 1


def test_ack_reflects_execution(ctl):
    ctl.apply_command(frame(CommandBit.START))
    ctl.step(1.0, [40.0] * 4)
    assert ctl.ack is AckStatus.EXECUTING

    ctl.apply_command(frame(CommandBit.STOP, seq=2))
    ctl.step(1.0, [40.0] * 4)
    assert ctl.ack is AckStatus.COMPLETED


def test_target_follows_mode(ctl):
    ctl.apply_command(frame(CommandBit.START, seq=1, setpoint=70.0, warm=40.0))
    assert ctl.target_c == 70.0
    ctl.apply_command(frame(CommandBit.WARM, seq=2, setpoint=70.0, warm=40.0))
    assert ctl.target_c == 40.0


# -- gateway link loss: warn, do not trip -----------------------------------


def test_link_loss_raises_a_warning_without_stopping(ctl):
    """A drying batch interrupted mid-cycle is scrap, so the chamber runs on."""
    ctl.apply_command(frame(CommandBit.START))
    for _ in range(int(ctl.min_on_off_sec) + 2):
        ctl.step(1.0, [40.0] * 4)
    assert ctl.heater_on is True

    ctl.gw_link_ok = False
    ctl.step(1.0, [40.0] * 4)

    assert ctl.alarm is AlarmCode.GW_LINK_LOST
    assert ctl.state is ChamberState.RUNNING, "must not stop"
    assert ctl.heater_on is True, "must keep heating"
    assert ctl.fault_latched is False, "warning, not a trip"


def test_link_warning_clears_itself(ctl):
    ctl.apply_command(frame(CommandBit.START))
    ctl.gw_link_ok = False
    ctl.step(1.0, [40.0] * 4)
    assert ctl.alarm is AlarmCode.GW_LINK_LOST

    ctl.gw_link_ok = True
    ctl.step(1.0, [40.0] * 4)
    assert ctl.alarm is AlarmCode.NONE


def test_real_fault_outranks_the_link_warning(ctl):
    """An overtemp must never be masked by a network problem."""
    ctl.apply_command(frame(CommandBit.START))
    ctl.gw_link_ok = False
    ctl.step(1.0, [96.0] * 4)

    assert ctl.alarm is AlarmCode.OVERTEMP
    assert ctl.state is ChamberState.FAULT
    assert ctl.fault_latched is True


def test_latched_fault_is_not_overwritten_by_link_state(ctl):
    ctl.apply_command(frame(CommandBit.START))
    ctl.step(1.0, [96.0] * 4)
    assert ctl.alarm is AlarmCode.OVERTEMP

    ctl.gw_link_ok = False
    ctl.step(1.0, [40.0] * 4)
    assert ctl.alarm is AlarmCode.OVERTEMP, "latched fault must keep its code"


def test_stop_still_works_while_the_link_is_down(ctl):
    ctl.apply_command(frame(CommandBit.START, seq=1))
    ctl.gw_link_ok = False
    ctl.step(1.0, [40.0] * 4)
    ctl.apply_command(frame(CommandBit.STOP, seq=2))
    assert ctl.state is ChamberState.STOPPED


def test_link_change_bumps_state_seq(ctl):
    """Clients poll state_seq to know something changed; this counts."""
    ctl.step(1.0, [40.0] * 4)
    before = ctl.state_seq
    ctl.gw_link_ok = False
    ctl.step(1.0, [40.0] * 4)
    assert ctl.state_seq == before + 1