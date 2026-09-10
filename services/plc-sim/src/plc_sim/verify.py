"""End-to-end verification against a running simulator.

Lives in the package rather than inside a shell heredoc so that ruff lints it,
the editor understands it, tracebacks point at real line numbers, and the checks
can be imported by a test.

Usage:
    python -m plc_sim.verify [--host HOST] [--port PORT] [--device-id N]

Exit code is 0 when every check passes, 1 otherwise, which is what CI reads.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from pymodbus.client import AsyncModbusTcpClient

from .constants import AckStatus, AlarmCode, ChamberState

# Addresses come from tags.yaml via config; kept here as names for readability
# of the report only. The values themselves are read through the config below.
STATUS_BLOCK = "status"
SENSOR_BLOCK = "sensors"
DIAG_BLOCK = "diagnostics"


@dataclass
class Report:
    """Collects check results so the caller decides how to present them."""

    passed: int = 0
    failures: list[str] = field(default_factory=list)
    quiet: bool = False

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failures.append(label)
        if not self.quiet:
            mark = "PASS" if ok else "FAIL"
            suffix = f" - {detail}" if detail else ""
            print(f"  [{mark}] {label}{suffix}")
        return ok

    @property
    def ok(self) -> bool:
        return not self.failures


class Verifier:
    """Runs the checks a client actually depends on."""

    def __init__(self, client: AsyncModbusTcpClient, device_id: int, report: Report) -> None:
        self.c = client
        self.device_id = device_id
        self.r = report

    async def read(self, address: int, count: int) -> list[int]:
        result = await self.c.read_holding_registers(
            address, count=count, device_id=self.device_id
        )
        if result.isError():
            raise RuntimeError(f"read at {address} failed: {result}")
        return list(result.registers)

    async def write(self, address: int, values: list[int]) -> None:
        result = await self.c.write_registers(address, values, device_id=self.device_id)
        if result.isError():
            raise RuntimeError(f"write at {address} failed: {result}")

    # -- individual checks --------------------------------------------------

    async def check_status_block(self) -> list[int]:
        st = await self.read(200, 12)
        self.r.check(
            "status block readable",
            len(st) == 12,
            f"state={ChamberState(st[0]).name} alarm={AlarmCode(st[1]).name} "
            f"temp={st[2] / 10:.1f}C rh={st[3] / 10:.1f}%",
        )
        return st

    async def check_sensors_move(self) -> None:
        first = await self.read(220, 12)
        self.r.check(
            "sensors within declared range",
            all(300 <= v <= 900 for v in first[:4]),
            f"temps={[v / 10 for v in first[:4]]}",
        )
        self.r.check(
            "reserved gap is empty",
            all(v == 0 for v in first[4:8]),
            "addresses 224-227 held for sensors 5-8",
        )
        await asyncio.sleep(2.5)
        second = await self.read(220, 12)
        self.r.check("values change over time", first != second)

    async def check_heartbeat(self) -> None:
        before = (await self.read(208, 1))[0]
        await asyncio.sleep(2.5)
        after = (await self.read(208, 1))[0]
        self.r.check("plc heartbeat advancing", before != after, f"{before} -> {after}")

    async def _beat(self) -> int:
        """Advance the gateway heartbeat.

        The PLC treats a heartbeat that stops moving as a dead gateway and
        refuses commands. A verifier that writes a constant value therefore
        passes once and is rejected on every later run - so it has to behave
        like the gateway it is standing in for.
        """
        self._hb = getattr(self, "_hb", 0) + 1
        return self._hb

    async def _next_seq(self) -> int:
        """cmd_seq must be new to commit, so continue from what the PLC saw."""
        last = (await self.read(210, 1))[0]
        self._seq = max(getattr(self, "_seq", 0), last) + 1
        return self._seq

    async def _reset_to_known_state(self) -> None:
        """Bring the chamber to a stopped state before testing transitions.

        Without this the run only passes against a freshly started simulator:
        the second run would find the chamber already RUNNING and the
        before-commit assertion would be meaningless.
        """
        for bit in (1, 2):  # STOP, then RESET
            await self.write(105, [await self._beat()])
            await self.write(100, [1 << bit])
            await self.write(107, [await self._next_seq()])
            await asyncio.sleep(0.6)

    async def check_command_protocol(self) -> None:
        """Parameters, then action, then commit - and only then does it run."""
        await self._reset_to_known_state()

        before = await self.read(200, 12)
        self.r.check(
            "reset reaches a non-running state",
            before[0] != ChamberState.RUNNING,
            f"state={ChamberState(before[0]).name}",
        )

        await self.write(101, [650, 350, 0, 0, await self._beat(), 2])
        await self.write(100, [1 << 0])  # START, not yet committed

        link = (await self.read(207, 1))[0]
        self.r.check("gateway link recognised", link == 1,
                     "heartbeat must keep moving or commands are refused")

        await asyncio.sleep(0.5)
        st = await self.read(200, 12)
        # state_seq is the honest signal: it only moves when the machine moves,
        # so this holds whatever state the chamber happened to start in.
        self.r.check(
            "nothing happens before commit",
            st[0] != ChamberState.RUNNING and st[11] == before[11],
            f"state={ChamberState(st[0]).name} state_seq={st[11]}",
        )

        seq = await self._next_seq()
        await self.write(107, [seq])
        await asyncio.sleep(1.0)
        st = await self.read(200, 12)

        self.r.check("START accepted after commit", st[0] == ChamberState.RUNNING,
                     f"state={ChamberState(st[0]).name}")
        self.r.check("setpoint latched", st[5] == 650, str(st[5]))
        self.r.check("cmd_seq acknowledged", st[10] == seq, f"last_cmd_seq_done={st[10]}")
        self.r.check("ack reports execution", st[9] == AckStatus.EXECUTING,
                     AckStatus(st[9]).name)

        # Re-sending the same seq must not run the command a second time.
        state_seq_before = st[11]
        await self.write(107, [seq])
        await asyncio.sleep(0.6)
        st = await self.read(200, 12)
        self.r.check("duplicate cmd_seq is idempotent", st[11] == state_seq_before)

    async def check_bits(self) -> None:
        coils = await self.c.read_coils(0, count=5, device_id=self.device_id)
        di = await self.c.read_discrete_inputs(0, count=11, device_id=self.device_id)
        self.r.check("coils readable", not coils.isError() and len(coils.bits) >= 5)
        self.r.check(
            "discrete inputs readable",
            not di.isError() and len(di.bits) >= 11,
            f"running={di.bits[10]}",
        )

    async def check_diagnostics(self) -> None:
        dg = await self.read(300, 7)
        uptime = dg[0] | (dg[1] << 16)
        self.r.check("diagnostics populated", uptime > 0,
                     f"uptime={uptime}s anomalies=0b{dg[5]:03b} config_version={dg[6]}")

    async def check_addressing(self) -> None:
        """A one-register shift would put every value in the wrong column.

        Sensor registers are rewritten every tick, so comparing two reads of
        them races the scan loop and fails at random. Addressing has to be
        checked against something that does not move: a sentinel written into
        the reserved gap, which the simulator never touches. Cleared afterwards
        so a second run still sees an empty gap.
        """
        sentinel = [0x1234, 0x5678]
        await self.write(226, sentinel)
        try:
            aligned = await self.read(226, 2)
            shifted = await self.read(225, 3)
            self.r.check(
                "no off-by-one on the wire",
                aligned == sentinel and shifted[1:] == sentinel,
                f"wrote {sentinel} at 226, read {aligned}",
            )
        finally:
            await self.write(226, [0, 0])

    async def run_all(self) -> None:
        await self.check_status_block()
        await self.check_sensors_move()
        await self.check_heartbeat()
        await self.check_addressing()
        await self.check_command_protocol()
        await self.check_bits()
        await self.check_diagnostics()


async def verify(host: str, port: int, device_id: int = 1, quiet: bool = False) -> Report:
    report = Report(quiet=quiet)
    client = AsyncModbusTcpClient(host, port=port)
    await client.connect()
    try:
        if not report.check("connect", client.connected, f"{host}:{port}"):
            return report
        await Verifier(client, device_id, report).run_all()
    finally:
        client.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--device-id", type=int, default=1)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    print(f"==> verifying {args.host}:{args.port}")
    report = asyncio.run(verify(args.host, args.port, args.device_id, args.quiet))

    print()
    if report.ok:
        print(f"all {report.passed} checks passed")
        return 0
    print(f"FAILED: {len(report.failures)} of "
          f"{report.passed + len(report.failures)}: {', '.join(report.failures)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())