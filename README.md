# factory-iiot-platform

An industrial IIoT platform built from the field device upward: simulated PLCs
speaking Modbus TCP, a collector, a broker, storage, and the operational layer
around them.

Rebuilt from scratch, informed by experience delivering a comparable system for
a real factory. The code, the register contract and the design decisions here
are all new; no customer material is included.

## Problem

A drying plant runs several chambers, each controlled by a PLC that speaks
Modbus TCP. The data exists but nobody can see it: no history, no alerting, no
way to tell a failing sensor from a normal one. Every diagnosis is someone
standing in front of the panel.

Building that visibility needs field hardware to develop against, which is the
one thing you cannot have on a laptop. So the platform starts with a simulator
faithful enough that everything downstream can be built and tested for real.

## Architecture

```
┌─────────────┐  Modbus TCP  ┌───────────┐  MQTT  ┌────────┐
│   plc-sim   │ ───────────▶ │ collector │ ─────▶ │ broker │ ─▶ storage ─▶ dashboards
│ 4 chambers  │    :5020     └───────────┘        └────────┘
└─────────────┘
       ▲
       └── services/plc-sim/config/tags.yaml — one contract, both sides
```

Full reasoning, address map and trade-offs: [`docs/architecture.md`](docs/architecture.md).

| Component | Status |
|---|---|
| `plc-sim` — Modbus TCP chamber simulator | working |
| collector — Modbus to MQTT | next |
| broker, storage, dashboards | planned |

## Run

```bash
make up        # four chambers in containers
make verify    # end-to-end check against chamber-01
make logs
make down
```

Without Docker:

```bash
make setup
make run       # one chamber on 127.0.0.1:5020
```

For an edge box with no container runtime, see
[`deploy/systemd/`](deploy/systemd/README.md) — a systemd template unit runs one
instance per chamber.

## Develop

```bash
make check     # lint, format check and tests
make scan      # nothing sensitive is tracked
make help
```

Before pushing, run `./scripts/dev-check.sh`. The full branch-to-merge workflow,
and what the local gates do not cover, is in
[`docs/contributing.md`](docs/contributing.md).

## What this demonstrates

- **A protocol contract as configuration.** Every address, scale, enum and
  block lives in `tags.yaml`. No address appears in Python, and adding a
  chamber or a sensor is a config change.
- **Atomic, idempotent commands over a protocol that has neither.** A commit
  register turns a multi-register write into a frame the device either sees
  whole or ignores, and a retry cannot execute twice.
- **Layering that makes testing cheap.** Only one module imports pymodbus, so
  the model and the state machine are tested in milliseconds without a socket.
- **Configuration validated at startup.** Duplicate addresses, overlapping
  blocks and enums that drifted out of sync with the code stop the process
  rather than corrupting data downstream.
- **Two deployment paths from one artefact.** Containers for development,
  a hardened systemd template unit for bare-metal edge hardware.
- **CI that checks what actually breaks:** the interface contract, the built
  image answering Modbus for real, unit-file syntax, and a secret scan.
