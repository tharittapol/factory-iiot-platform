"""The verification suite is importable, so it can be tested like any module.

That is the point of moving it out of a shell heredoc: these two tests would be
impossible against an embedded script.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from plc_sim.main import ChamberSimulator
from plc_sim.verify import Report, verify

PORT = 5601


@pytest.fixture
async def running_sim(cfg):
    sim = ChamberSimulator(cfg, "test-chamber", "127.0.0.1", PORT)
    task = asyncio.create_task(sim.run())
    await asyncio.sleep(0.6)
    try:
        yield sim
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_report_counts_and_reports():
    r = Report(quiet=True)
    r.check("a", True)
    r.check("b", False)
    assert r.passed == 1
    assert r.failures == ["b"]
    assert r.ok is False


@pytest.mark.asyncio
async def test_verify_passes_against_a_live_simulator(running_sim):
    report = await verify("127.0.0.1", PORT, device_id=1, quiet=True)
    assert report.ok, f"failed checks: {report.failures}"
    assert report.passed >= 15


@pytest.mark.asyncio
async def test_verify_reports_failure_when_nothing_is_listening():
    report = await verify("127.0.0.1", 5999, quiet=True)
    assert not report.ok
    assert "connect" in report.failures


def test_unknown_argument_is_rejected():
    """Silently ignoring an operator's flag is worse than failing."""
    from plc_sim.main import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--hostt", "chamber-03"])
    with pytest.raises(SystemExit):
        parse_args(["chamber-03", "5020"])


def test_cli_overrides_environment(monkeypatch, tags_file):
    from plc_sim.main import build_simulator, parse_args

    monkeypatch.setenv("CHAMBER_ID", "from-env")
    monkeypatch.setenv("BIND_PORT", "5555")
    args = parse_args(["--chamber-id", "from-cli", "--port", "5556",
                       "--tags", str(tags_file)])
    sim = build_simulator(args)
    assert sim.chamber_id == "from-cli"
    assert sim.mb.port == 5556


def test_environment_used_when_no_flag(monkeypatch, tags_file):
    from plc_sim.main import build_simulator, parse_args

    monkeypatch.setenv("CHAMBER_ID", "from-env")
    monkeypatch.setenv("BIND_PORT", "5557")
    sim = build_simulator(parse_args(["--tags", str(tags_file)]))
    assert sim.chamber_id == "from-env"
    assert sim.mb.port == 5557