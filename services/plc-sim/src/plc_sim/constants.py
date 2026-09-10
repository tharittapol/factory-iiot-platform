"""Protocol constants.

These mirror the ``enums`` and ``command_bits`` sections of tags.yaml.
config.validate() checks that the two stay in sync, so a rename in the YAML
fails fast at startup instead of silently drifting from the code.
"""

from __future__ import annotations

from enum import IntEnum


class ChamberState(IntEnum):
    IDLE = 0
    READY = 1
    RUNNING = 2
    WARM_HOLD = 3
    STOPPED = 4
    FAULT = 5
    WAITING = 6
    PAUSED = 7


class AlarmCode(IntEnum):
    NONE = 0
    OVERTEMP = 1
    SENSOR_FAIL = 2
    E_STOP = 3
    HEATER_FAIL = 4
    FAN_FAIL = 5


class AckStatus(IntEnum):
    ACCEPTED = 0
    REJECTED = 1
    EXECUTING = 2
    COMPLETED = 3
    FAILED = 4
    NONE = 5


class DesiredMode(IntEnum):
    NONE = 0
    WAIT = 1
    RUN = 2
    WARM = 3


class CommandBit(IntEnum):
    """Bit positions inside holding register 100 (cmd_word)."""

    START = 0
    STOP = 1
    RESET = 2
    ACK_ALARM = 3
    WARM = 4
    PAUSE = 5
    RESUME = 6
    WAIT = 7
    SET = 8


# Modbus function codes, kept in one place so no bare 1/2/3/4 appears elsewhere.
FC_READ_COILS = 1
FC_READ_DISCRETE_INPUTS = 2
FC_READ_HOLDING = 3
FC_READ_INPUT = 4

FUNCTION_BY_NAME = {
    "coil": FC_READ_COILS,
    "discrete_input": FC_READ_DISCRETE_INPUTS,
    "holding": FC_READ_HOLDING,
    "input": FC_READ_INPUT,
}

# Registers are unsigned 16-bit; counters wrap below this to stay positive
# even if a client mistakenly decodes them as signed.
S16_MAX = 32767


def next_counter(value: int) -> int:
    """Increment a heartbeat/sequence counter, wrapping 1..S16_MAX."""
    return 1 if value >= S16_MAX else value + 1
