"""Bridge between engineering values and Modbus registers.

Everything above this module works in degC and %RH. Everything below works in
unsigned 16-bit words. This is the only file that crosses that line, and the
only one that imports pymodbus.

Written against pymodbus 3.15, where ModbusServerContext and
ModbusDeviceContext are deprecated: the current API builds a SimDevice from
SimData blocks and reads or writes through the server object itself. Addresses
are raw PDU addresses with no zero_mode adjustment - what you write at 100 is
what a client reads at 100.
"""

from __future__ import annotations

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from .config import Config
from .constants import (
    FC_READ_COILS,
    FC_READ_DISCRETE_INPUTS,
    FC_READ_HOLDING,
    FC_READ_INPUT,
)


class ModbusMap:
    """Owns the datastore layout and all value conversion."""

    def __init__(self, cfg: Config, host: str | None = None, port: int | None = None) -> None:
        self.cfg = cfg
        self.device_id = cfg.device.unit_id
        self.host = host or cfg.device.host
        self.port = port or cfg.device.port
        self.server: ModbusTcpServer | None = None
        self._sim_device = self._build_device()

    # -- datastore ----------------------------------------------------------

    def _build_device(self) -> SimDevice:
        """Size each block from the tag map, with headroom for reserved ranges."""
        hr_size = self._round_up(self.cfg.max_address("holding") + 1, 64)
        ir_size = max(8, self.cfg.max_address("input") + 1)
        co_size = self._round_up(self.cfg.max_address("coil") + 1, 16)
        di_size = self._round_up(self.cfg.max_address("discrete_input") + 1, 16)

        coils = [SimData(0, count=co_size, datatype=DataType.BITS)]
        discrete = [SimData(0, count=di_size, datatype=DataType.BITS)]
        holding = [SimData(0, count=hr_size, datatype=DataType.REGISTERS)]
        inputs = [SimData(0, count=ir_size, datatype=DataType.REGISTERS)]

        # The 4-tuple form gives each function its own address space, matching
        # a classic PLC. A single shared list would make coil 0 and holding 0
        # the same storage, which is not what tags.yaml describes.
        return SimDevice(id=self.device_id, simdata=(coils, discrete, holding, inputs))

    @staticmethod
    def _round_up(value: int, multiple: int) -> int:
        return ((value + multiple - 1) // multiple) * multiple

    def build_server(self) -> ModbusTcpServer:
        self.server = ModbusTcpServer(self._sim_device, address=(self.host, self.port))
        return self.server

    # -- raw access ---------------------------------------------------------

    async def _write(self, fc: int, address: int, values: list) -> None:
        assert self.server is not None, "build_server() must be called first"
        await self.server.async_setValues(self.device_id, fc, address, values)

    async def _read(self, fc: int, address: int, count: int) -> list:
        assert self.server is not None, "build_server() must be called first"
        return await self.server.async_getValues(self.device_id, fc, address, count)

    # -- typed access -------------------------------------------------------

    async def write_tag(self, name: str, value) -> None:
        """Write one tag using its declared scale and datatype."""
        tag = self.cfg.tag(name)
        if tag.is_bit:
            await self._write(tag.function_code, tag.address, [bool(value)])
            return
        if tag.datatype == "uint32":
            await self._write(FC_READ_HOLDING, tag.address, self._split_u32(int(value), tag.word_order))
            return
        raw = self.cfg.to_raw(name, float(value)) if tag.scale != 1.0 else int(value)
        await self._write(FC_READ_HOLDING, tag.address, [max(0, min(65535, raw))])

    async def read_tag(self, name: str) -> float | int | bool:
        tag = self.cfg.tag(name)
        if tag.is_bit:
            bits = await self._read(tag.function_code, tag.address, 1)
            return bool(bits[0])
        if tag.datatype == "uint32":
            words = await self._read(FC_READ_HOLDING, tag.address, 2)
            return self._join_u32(list(words), tag.word_order)
        words = await self._read(FC_READ_HOLDING, tag.address, 1)
        raw = int(words[0])
        return self.cfg.to_eng(name, raw) if tag.scale != 1.0 else raw

    async def write_many(self, values: dict[str, object]) -> None:
        for name, value in values.items():
            await self.write_tag(name, value)

    async def read_block_raw(self, block_name: str) -> list[int]:
        block = self.cfg.block(block_name)
        return list(await self._read(block.function_code, block.start, block.count))

    # -- uint32 helpers -----------------------------------------------------

    @staticmethod
    def _split_u32(value: int, word_order: str) -> list[int]:
        value &= 0xFFFFFFFF
        low, high = value & 0xFFFF, (value >> 16) & 0xFFFF
        return [low, high] if word_order == "little" else [high, low]

    @staticmethod
    def _join_u32(words: list[int], word_order: str) -> int:
        low, high = (words[0], words[1]) if word_order == "little" else (words[1], words[0])
        return (high << 16) | low


__all__ = [
    "ModbusMap",
    "FC_READ_COILS",
    "FC_READ_DISCRETE_INPUTS",
    "FC_READ_HOLDING",
    "FC_READ_INPUT",
]
