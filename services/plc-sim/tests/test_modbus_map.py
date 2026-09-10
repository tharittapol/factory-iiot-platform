"""Tests for the register bridge, including the end-to-end wire path.

The off-by-one test is the important one here: if what the simulator writes at
address 220 is not what a client reads at 220, every downstream value lands in
the wrong column and the bug looks like bad data rather than bad addressing.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from pymodbus.client import AsyncModbusTcpClient

from plc_sim.modbus_map import ModbusMap

PORT = 5599


@pytest.fixture
async def running_map(cfg):
    mb = ModbusMap(cfg, host="127.0.0.1", port=PORT)
    server = mb.build_server()
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.3)
    try:
        yield mb
    finally:
        await server.shutdown()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_scaled_write_lands_in_register(running_map):
    await running_map.write_tag("setpoint_x10", 65.0)
    raw = await running_map._read(3, 101, 1)
    assert raw[0] == 650


@pytest.mark.asyncio
async def test_round_trip_through_tag_api(running_map):
    await running_map.write_tag("temp_1_x10", 65.3)
    assert await running_map.read_tag("temp_1_x10") == pytest.approx(65.3, abs=0.05)


@pytest.mark.asyncio
async def test_uint32_word_order(running_map):
    await running_map.write_tag("uptime_sec", 70000)
    low, high = await running_map._read(3, 300, 2)
    assert low == 70000 & 0xFFFF
    assert high == 70000 >> 16
    assert await running_map.read_tag("uptime_sec") == 70000


@pytest.mark.asyncio
async def test_bits_are_separate_address_space(running_map):
    """coil 0 and holding 0 must not be the same storage."""
    await running_map.write_tag("local_start", True)
    await running_map._write(3, 0, [1234])
    assert await running_map.read_tag("local_start") is True
    assert (await running_map._read(3, 0, 1))[0] == 1234


@pytest.mark.asyncio
async def test_no_off_by_one_on_the_wire(running_map, cfg):
    """What we write at N is what a client reads at N."""
    await running_map._write(3, 220, [1111, 2222])

    client = AsyncModbusTcpClient("127.0.0.1", port=PORT)
    await client.connect()
    try:
        result = await client.read_holding_registers(
            220, count=2, device_id=cfg.device.unit_id
        )
        assert result.registers == [1111, 2222]

        shifted = await client.read_holding_registers(
            219, count=3, device_id=cfg.device.unit_id
        )
        assert shifted.registers == [0, 1111, 2222]
    finally:
        client.close()


@pytest.mark.asyncio
async def test_block_read_matches_tag_addresses(running_map, cfg):
    await running_map.write_tag("chamber_state", 2)
    await running_map.write_tag("plc_heartbeat", 42)
    values = await running_map.read_block_raw("status")

    block = cfg.block("status")
    assert len(values) == block.count
    assert values[cfg.address_of("chamber_state") - block.start] == 2
    assert values[cfg.address_of("plc_heartbeat") - block.start] == 42
