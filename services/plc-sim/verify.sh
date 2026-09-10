#!/usr/bin/env bash
# End-to-end check against a running simulator.
# Usage: ./verify.sh [host] [port]
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-5020}"

echo "==> verifying ${HOST}:${PORT}"
python3 - "$HOST" "$PORT" <<'PY'
import asyncio, sys
from pymodbus.client import AsyncModbusTcpClient

HOST, PORT = sys.argv[1], int(sys.argv[2])
STATES = ["IDLE","READY","RUNNING","WARM_HOLD","STOPPED","FAULT","WAITING","PAUSED"]
ALARMS = ["NONE","OVERTEMP","SENSOR_FAIL","E_STOP","HEATER_FAIL","FAN_FAIL"]
failures = []

def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{' - ' + detail if detail else ''}")
    if not ok:
        failures.append(label)

async def main():
    c = AsyncModbusTcpClient(HOST, port=PORT)
    await c.connect()
    check("connect", c.connected)

    st = (await c.read_holding_registers(200, count=12, device_id=1)).registers
    check("status block readable", len(st) == 12,
          f"state={STATES[st[0]]} alarm={ALARMS[st[1]]} temp={st[2]/10:.1f}C rh={st[3]/10:.1f}%")

    s0 = (await c.read_holding_registers(220, count=12, device_id=1)).registers
    check("sensors in range", all(300 <= v <= 900 for v in s0[:4]),
          f"temps={[v/10 for v in s0[:4]]}")

    await asyncio.sleep(2.5)
    s1 = (await c.read_holding_registers(220, count=12, device_id=1)).registers
    check("values change over time", s0 != s1)

    hb0 = (await c.read_holding_registers(208, count=1, device_id=1)).registers[0]
    await asyncio.sleep(2.5)
    hb1 = (await c.read_holding_registers(208, count=1, device_id=1)).registers[0]
    check("plc heartbeat advancing", hb1 != hb0, f"{hb0} -> {hb1}")

    # Command frame: parameters, action, commit.
    await c.write_registers(101, [650, 350, 0, 0, 1, 2], device_id=1)
    await c.write_register(100, 1, device_id=1)
    await c.write_register(107, 99, device_id=1)
    await asyncio.sleep(1.0)

    st = (await c.read_holding_registers(200, count=12, device_id=1)).registers
    check("START accepted", st[0] == 2, f"state={STATES[st[0]]}")
    check("setpoint latched", st[5] == 650, f"{st[5]}")
    check("cmd_seq acknowledged", st[10] == 99, f"last_cmd_seq_done={st[10]}")

    # Re-committing the same seq must not re-execute.
    seq_before = st[11]
    await c.write_register(107, 99, device_id=1)
    await asyncio.sleep(0.6)
    st2 = (await c.read_holding_registers(200, count=12, device_id=1)).registers
    check("duplicate cmd_seq is idempotent", st2[11] == seq_before)

    co = (await c.read_coils(0, count=5, device_id=1)).bits[:5]
    di = (await c.read_discrete_inputs(0, count=11, device_id=1)).bits[:11]
    check("coils readable", len(co) == 5)
    check("discrete inputs readable", len(di) == 11, f"running={di[10]}")

    dg = (await c.read_holding_registers(300, count=7, device_id=1)).registers
    check("diagnostics populated", dg[0] > 0, f"uptime={dg[0]}s anomalies=0b{dg[5]:03b}")

    c.close()

asyncio.run(main())
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
PY
