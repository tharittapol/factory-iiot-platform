# plc-sim

Modbus TCP simulator for an industrial drying chamber PLC.

One process serves one chamber on one TCP endpoint, mirroring real hardware
where each PLC owns an IP address. The entire register contract lives in
`config/tags.yaml`; no address appears in Python.

## Run

```bash
uv sync
uv run python -m plc_sim.main
```

Environment:

| Variable | Default | Purpose |
|---|---|---|
| `TAGS_FILE` | `config/tags.yaml` | tag map to load |
| `CHAMBER_ID` | `chamber-01` | seeds the model so chambers differ |
| `BIND_HOST` / `BIND_PORT` | from `tags.yaml` | listener address |
| `LOG_LEVEL` | `INFO` | |
| `INJECT_SPIKE` / `INJECT_STUCK` / `INJECT_DRIFT` | off | fault injection |

## Verify

```bash
uv run pytest
./verify.sh          # end-to-end against a running instance
```

With `mbpoll` (1-based addressing, so PDU 220 is `-r 221`):

```bash
mbpoll -m tcp -a 1 -r 221 -c 12 -t 4 -1 127.0.0.1 -p 5020   # sensors
mbpoll -m tcp -a 1 -r 201 -c 12 -t 4 -l 1000 127.0.0.1 -p 5020   # status, live
```

Send a command (parameters, then action, then commit):

```bash
mbpoll -m tcp -a 1 -r 102 -t 4 127.0.0.1 -p 5020 650 350 0 0 1 2
mbpoll -m tcp -a 1 -r 101 -t 4 127.0.0.1 -p 5020 1
mbpoll -m tcp -a 1 -r 108 -t 4 127.0.0.1 -p 5020 1
```

Register 200 should now read `2` (RUNNING).

See `docs/architecture.md` for the address map and the reasoning behind it.
