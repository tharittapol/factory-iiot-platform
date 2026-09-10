# Architecture

## Provenance

This project was built from scratch. It draws on experience delivering an IIoT
system for a real factory, but the code, the register contract and the design
decisions here are all new. No customer material is included.

## Scope of this document

The simulator is the first service in the platform. It exists so that the rest
of the stack — collector, broker, storage, dashboards, alerting — can be built
and tested without hardware, and so the addressing contract is written down
once and consumed by everything.

```
┌────────────┐   Modbus TCP    ┌───────────┐   MQTT   ┌─────────┐
│  plc-sim   │ ──────────────▶ │ collector │ ───────▶ │ broker  │ ─▶ storage
│ (4 chambers)│   :5020         └───────────┘          └─────────┘
└────────────┘
      ▲
      └── config/tags.yaml is the single source of truth for both sides
```

## Address map

All addresses are raw PDU addresses, zero-based. There is no `4xxxx` offset and
no `zero_mode` adjustment: what the simulator writes at 220 is what a client
reads at 220. `tests/test_modbus_map.py::test_no_off_by_one_on_the_wire` asserts
this against a real client on a real socket.

| Range | Block | Direction | Notes |
|---|---|---|---|
| 100–107 | command | gateway → PLC | 108–115 reserved |
| 200–211 | status | PLC → gateway | 212–215 reserved |
| 220–223 | temperature 1–4 | PLC → gateway | 224–227 reserved for sensors 5–8 |
| 228–231 | humidity 1–4 | PLC → gateway | 232–235 reserved |
| 300–306 | diagnostics | PLC → gateway | 307–311 reserved |
| coils 0–4 | local panel | bidirectional | |
| discrete inputs 0–10 | panel status | PLC → gateway | |

### Why the blocks are grouped this way

A poll cycle should cost a fixed, small number of round trips. Status is twelve
contiguous registers so one `read_holding_registers(200, 12)` returns the entire
control state. Temperature and humidity sit close enough that
`read_holding_registers(220, 12)` collects all eight sensors in one request; the
four reserved registers in the middle are read and discarded, which is cheaper
than a second round trip. At 1 Hz across four chambers that is 8 requests per
second rather than 80.

### Why reserved ranges exist

Adding a fifth temperature sensor must not move the humidity registers. If it
did, every client would have to be updated in lockstep with the PLC — the same
problem as breaking an API contract. The gaps make growth backward compatible.

## Why `cmd_seq` instead of coils

Modbus has no transactions. A command here is six parameters plus an action,
and a client that writes them one at a time can be interrupted halfway: the PLC
would see a new setpoint with a stale step index and act on the mixture.

The command frame is therefore written in a fixed order:

1. `FC16` writes 101–106 — every parameter, in one atomic request
2. `FC06` writes 100 — `cmd_word`, the action bitmask
3. `FC06` writes 107 — `cmd_seq`, the commit

The PLC evaluates the frame **only when `cmd_seq` changes**. Until then the new
parameters sit in registers doing nothing, so a failure during step 1 or 2 is
harmless: the client simply starts over. Writing the same `cmd_seq` again does
not re-execute the command, which makes retries safe.

This gives two properties that coils cannot:

- **Atomicity** — the frame is seen whole or not at all.
- **Idempotency** — a retry after a timeout cannot double-start a job.

Coils are still used, but only for the local panel (`local_start`,
`local_stop`, `alarm_ack`, `lamp_test`, `maintenance_mode`). A panel button
carries no parameters, so it has nothing to make atomic.

`last_cmd_seq_done` closes the loop: the gateway waits for it to equal the
`cmd_seq` it sent before reading `ack_status`, so it never interprets an ack
from the previous command.

## Why holding registers carry read-only data

Strictly, sensor readings belong in input registers (FC4). They are in holding
registers because most HMI and SCADA software supports FC3 best, some does not
implement FC4 at all, and mainstream PLCs already map their data registers to
the holding space. Polling one function code also keeps the collector simple.

The cost is that nothing at the protocol level stops a client from writing to a
sensor register. Two things mitigate it: `access: r` in `tags.yaml` states the
contract, and the simulator rewrites every sensor register on each scan, so a
stray write disappears within one tick.

## Why one container per chamber

A Modbus TCP device is an IP address listening on a port. Four chambers means
four addresses, so the simulator runs four containers rather than one process
binding four ports. That has three consequences worth having:

- The endpoint is configuration, not code. Switching to real hardware changes
  the collector's endpoint list and nothing else.
- Failure is testable. `docker stop chamber-02` is a PLC going offline, and the
  collector's reconnect logic gets exercised for real.
- The simulator stays simple: no threads, no multi-device context, one scan
  loop per process.

The number of chambers lives in `tags.yaml` and `compose.yaml`. It never
appears in Python.

## Module boundaries

| Module | Responsibility | Knows about Modbus? |
|---|---|---|
| `config.py` | load and validate `tags.yaml`, scaling | no |
| `physics.py` | thermal and moisture model | no |
| `state_machine.py` | states, interlocks, actuator logic | no |
| `anomaly.py` | fault injection | no |
| `modbus_map.py` | engineering values ↔ registers | **yes, only here** |
| `main.py` | wiring and the scan loop | via `modbus_map` |

Only `modbus_map.py` imports pymodbus. Everything above it works in degC and
%RH, which is why the physics and state machine tests run in milliseconds
without opening a socket.

`config.py` is the only place that knows about `scale`. A value is converted at
the boundary and nowhere else, so a `x10` bug cannot hide in business logic.

## Configuration is validated at startup

`load_config` rejects duplicate addresses, unknown enum references, overlapping
blocks, non-positive scales and blocks that cover no tags. It also checks that
the enums in `tags.yaml` match the `IntEnum` classes in `constants.py`: if
someone renames a state in one place and not the other, the process refuses to
start rather than emitting a value whose meaning has quietly changed.

## Anomaly injection

Three modes, chosen because they need different detection strategies:

| Mode | Behaviour | Caught by |
|---|---|---|
| `spike` | brief excursion past the trip point | threshold alarm |
| `stuck` | reading freezes at a legal value | variance check |
| `drift` | slow creep, always in range | neither — needs statistics |

`drift` is the reason the alerting work later is worth doing. A threshold never
fires, and a variance check sees healthy noise. Detecting it requires comparing
a sensor against its own history or against its neighbours, which is a genuine
data problem rather than a rule.

`chamber-03` runs with `INJECT_DRIFT=true` by default so the stack always has
one real anomaly to find. Register 305 reports which modes are active, so a
consumer can tell an injected fault from a modelling artefact.

## Scan order

The tick follows what a PLC scan does, and the order is load-bearing:

```
read gateway heartbeat        → decide link state
advance own heartbeat
read cmd_seq                  → act only if it changed
run the state machine
step the physics
sample sensors, inject anomalies
check safety
publish status, sensors, diagnostics
```

Safety runs after sampling so a trip uses the current scan's readings, and
publishing runs last so a client never sees a half-updated status block.

## Known limitations

- Registers are unsigned 16-bit. Sub-zero temperatures would need `int16` and a
  two's-complement path in `modbus_map.py`.
- There is no authentication. Modbus TCP has none; in production the segment is
  isolated and the gateway is the only route in.
- The `sim.expr` fields in `tags.yaml` are documentation, not an expression
  language. Derived values are computed in Python on purpose — a small
  expression engine in YAML is a week of work that buys nothing.
