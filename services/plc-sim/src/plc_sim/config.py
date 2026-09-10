"""Load and validate tags.yaml.

This module is the only place that knows about scaling. Everything else in the
simulator works in engineering units (degC, %RH) and calls to_raw/to_eng at the
boundary.

A bad config must fail here, at startup, with a message that names the tag.
Discovering a duplicate address three hours into a run is not acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .constants import (
    FUNCTION_BY_NAME,
    AckStatus,
    AlarmCode,
    ChamberState,
    CommandBit,
    DesiredMode,
)


class ConfigError(ValueError):
    """Raised when tags.yaml is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Tag:
    address: int
    name: str
    function: str  # holding | coil | discrete_input | input
    datatype: str = "uint16"
    encoding: str | None = None  # enum | bitmask | bool | None
    enum: str | None = None
    unit: str | None = None
    scale: float = 1.0
    access: str = "r"
    registers: int = 1
    word_order: str = "little"
    value_range: tuple[int, int] | None = None
    sim: dict[str, Any] = field(default_factory=dict)
    description: str | None = None

    @property
    def function_code(self) -> int:
        return FUNCTION_BY_NAME[self.function]

    @property
    def is_bit(self) -> bool:
        return self.function in ("coil", "discrete_input")


@dataclass(frozen=True)
class Block:
    name: str
    function: str
    start: int
    count: int
    access: str = "r"
    note: str | None = None

    @property
    def end(self) -> int:
        """Last address covered by this block, inclusive."""
        return self.start + self.count - 1

    @property
    def function_code(self) -> int:
        return FUNCTION_BY_NAME[self.function]

    def covers(self, address: int) -> bool:
        return self.start <= address <= self.end


@dataclass(frozen=True)
class Device:
    model: str
    host: str
    port: int
    unit_id: int
    description: str = ""


@dataclass
class Config:
    version: int
    device: Device
    enums: dict[str, dict[int, str]]
    command_bits: dict[int, str]
    blocks: dict[str, Block]
    tags: dict[str, Tag]  # keyed by name
    simulation: dict[str, Any]
    anomalies: dict[str, dict[str, Any]]
    source_path: Path

    # -- lookups ------------------------------------------------------------

    def tag(self, name: str) -> Tag:
        try:
            return self.tags[name]
        except KeyError:
            raise ConfigError(f"unknown tag: {name!r}") from None

    def address_of(self, name: str) -> int:
        return self.tag(name).address

    def block(self, name: str) -> Block:
        try:
            return self.blocks[name]
        except KeyError:
            raise ConfigError(f"unknown block: {name!r}") from None

    def tags_in(self, block_name: str) -> list[Tag]:
        b = self.block(block_name)
        return sorted(
            (t for t in self.tags.values() if t.function == b.function and b.covers(t.address)),
            key=lambda t: t.address,
        )

    def tags_by_function(self, function: str) -> list[Tag]:
        return sorted(
            (t for t in self.tags.values() if t.function == function),
            key=lambda t: t.address,
        )

    def max_address(self, function: str) -> int:
        tags = self.tags_by_function(function)
        if not tags:
            return 0
        return max(t.address + t.registers - 1 for t in tags)

    # -- scaling ------------------------------------------------------------

    def to_raw(self, name: str, value: float) -> int:
        """Engineering value -> register value, clamped to the tag's range."""
        tag = self.tag(name)
        raw = int(round(value / tag.scale))
        if tag.value_range:
            lo, hi = tag.value_range
            raw = max(lo, min(hi, raw))
        return max(0, min(65535, raw))

    def to_eng(self, name: str, raw: int) -> float:
        """Register value -> engineering value."""
        return raw * self.tag(name).scale


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_ENUM_CLASSES = {
    "chamber_state": ChamberState,
    "alarm_code": AlarmCode,
    "ack_status": AckStatus,
    "desired_mode": DesiredMode,
}

_REQUIRED_SECTIONS = (
    "version",
    "device",
    "enums",
    "command_bits",
    "blocks",
    "holding_registers",
    "coils",
    "discrete_inputs",
    "simulation",
)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"tags file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    missing = [s for s in _REQUIRED_SECTIONS if s not in raw]
    if missing:
        raise ConfigError(f"{path}: missing section(s): {', '.join(missing)}")

    device = _build_device(raw["device"])
    blocks = _build_blocks(raw["blocks"])
    tags = _build_tags(raw)

    cfg = Config(
        version=int(raw["version"]),
        device=device,
        enums={k: {int(kk): vv for kk, vv in v.items()} for k, v in raw["enums"].items()},
        command_bits={int(k): v for k, v in raw["command_bits"].items()},
        blocks=blocks,
        tags=tags,
        simulation=raw["simulation"],
        anomalies=raw.get("anomalies", {}),
        source_path=path,
    )
    _validate(cfg)
    return cfg


def _build_device(d: dict[str, Any]) -> Device:
    for key in ("port", "unit_id"):
        if key not in d:
            raise ConfigError(f"device: missing {key}")
    return Device(
        model=d.get("model", "unknown"),
        host=d.get("host", "0.0.0.0"),
        port=int(d["port"]),
        unit_id=int(d["unit_id"]),
        description=d.get("description", ""),
    )


def _build_blocks(raw: dict[str, Any]) -> dict[str, Block]:
    blocks: dict[str, Block] = {}
    for name, b in raw.items():
        fn = b.get("function")
        if fn not in FUNCTION_BY_NAME:
            raise ConfigError(f"block {name!r}: unknown function {fn!r}")
        blocks[name] = Block(
            name=name,
            function=fn,
            start=int(b["start"]),
            count=int(b["count"]),
            access=b.get("access", "r"),
            note=b.get("note"),
        )
    return blocks


def _build_tags(raw: dict[str, Any]) -> dict[str, Tag]:
    tags: dict[str, Tag] = {}
    sections = (
        ("holding_registers", "holding"),
        ("coils", "coil"),
        ("discrete_inputs", "discrete_input"),
        ("input_registers", "input"),
    )
    for section, function in sections:
        for entry in raw.get(section) or []:
            name = entry.get("name")
            if not name:
                raise ConfigError(f"{section}: an entry has no name")
            if name in tags:
                raise ConfigError(f"duplicate tag name: {name!r}")

            rng = entry.get("range")
            if rng is not None:
                if not isinstance(rng, list) or len(rng) != 2:
                    raise ConfigError(f"tag {name!r}: range must be [min, max]")
                if rng[0] >= rng[1]:
                    raise ConfigError(f"tag {name!r}: range min must be < max")
                rng = (int(rng[0]), int(rng[1]))

            scale = float(entry.get("scale", 1.0))
            if scale <= 0:
                raise ConfigError(f"tag {name!r}: scale must be > 0")

            tags[name] = Tag(
                address=int(entry["address"]),
                name=name,
                function=function,
                datatype=entry.get("datatype", "bool" if function != "holding" else "uint16"),
                encoding=entry.get("encoding"),
                enum=entry.get("enum"),
                unit=entry.get("unit"),
                scale=scale,
                access=entry.get("access", "r"),
                registers=int(entry.get("registers", 1)),
                word_order=entry.get("word_order", "little"),
                value_range=rng,
                sim=entry.get("sim") or {},
                description=entry.get("description"),
            )
    return tags


def _validate(cfg: Config) -> None:
    _check_duplicate_addresses(cfg)
    _check_enum_refs(cfg)
    _check_enums_match_code(cfg)
    _check_command_bits(cfg)
    _check_blocks(cfg)


def _check_duplicate_addresses(cfg: Config) -> None:
    for function in FUNCTION_BY_NAME:
        seen: dict[int, str] = {}
        for tag in cfg.tags_by_function(function):
            for offset in range(tag.registers):
                addr = tag.address + offset
                if addr in seen:
                    raise ConfigError(
                        f"{function} address {addr} used by both "
                        f"{seen[addr]!r} and {tag.name!r}"
                    )
                seen[addr] = tag.name


def _check_enum_refs(cfg: Config) -> None:
    for tag in cfg.tags.values():
        if tag.encoding != "enum":
            continue
        if not tag.enum:
            raise ConfigError(f"tag {tag.name!r}: encoding=enum but no enum given")
        if tag.enum not in cfg.enums:
            raise ConfigError(f"tag {tag.name!r}: unknown enum {tag.enum!r}")


def _check_enums_match_code(cfg: Config) -> None:
    """The YAML and constants.py must agree, or the wire protocol lies."""
    for enum_name, cls in _ENUM_CLASSES.items():
        if enum_name not in cfg.enums:
            raise ConfigError(f"enums: missing {enum_name!r}")
        yaml_map = cfg.enums[enum_name]
        code_map = {m.value: m.name for m in cls}
        if yaml_map != code_map:
            raise ConfigError(
                f"enum {enum_name!r} in {cfg.source_path.name} does not match "
                f"constants.{cls.__name__}: yaml={yaml_map} code={code_map}"
            )


def _check_command_bits(cfg: Config) -> None:
    code_map = {m.value: m.name for m in CommandBit}
    if cfg.command_bits != code_map:
        raise ConfigError(
            f"command_bits do not match constants.CommandBit: "
            f"yaml={cfg.command_bits} code={code_map}"
        )


def _check_blocks(cfg: Config) -> None:
    for name, block in cfg.blocks.items():
        if block.count <= 0:
            raise ConfigError(f"block {name!r}: count must be > 0")
        if not cfg.tags_in(name):
            raise ConfigError(f"block {name!r} covers no tags")

    # Blocks of the same function must not overlap; overlap means a single
    # read could be served by two different block definitions.
    by_function: dict[str, list[Block]] = {}
    for block in cfg.blocks.values():
        by_function.setdefault(block.function, []).append(block)
    for function, blocks in by_function.items():
        ordered = sorted(blocks, key=lambda b: b.start)
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if nxt.start <= prev.end:
                raise ConfigError(
                    f"{function} blocks {prev.name!r} [{prev.start}..{prev.end}] and "
                    f"{nxt.name!r} [{nxt.start}..{nxt.end}] overlap"
                )