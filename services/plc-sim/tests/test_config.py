"""Tests for config loading and validation."""

from __future__ import annotations

import textwrap

import pytest
import yaml

from plc_sim.config import ConfigError, load_config


def test_loads_real_file(cfg):
    assert cfg.version >= 1
    assert cfg.device.port == 5020
    assert cfg.device.unit_id == 1
    assert len(cfg.tags) > 30


def test_scaling_round_trip(cfg):
    assert cfg.to_raw("setpoint_x10", 65.0) == 650
    assert cfg.to_eng("temp_1_x10", 653) == pytest.approx(65.3)
    assert cfg.to_eng("rh_1_x10", 725) == pytest.approx(72.5)


def test_scaling_clamps_to_declared_range(cfg):
    # setpoint range is [300, 900] in raw units
    assert cfg.to_raw("setpoint_x10", 5.0) == 300
    assert cfg.to_raw("setpoint_x10", 250.0) == 900


def test_address_lookup(cfg):
    assert cfg.address_of("cmd_seq") == 107
    assert cfg.address_of("chamber_state") == 200
    assert cfg.address_of("temp_1_x10") == 220


def test_status_block_is_contiguous_and_complete(cfg):
    block = cfg.block("status")
    tags = cfg.tags_in("status")
    assert len(tags) == block.count, "status block must have no holes"
    addresses = [t.address for t in tags]
    assert addresses == list(range(block.start, block.start + block.count))


def test_sensor_block_covers_temp_and_rh(cfg):
    names = [t.name for t in cfg.tags_in("sensors")]
    assert "temp_1_x10" in names and "rh_4_x10" in names
    # 12 registers cover 8 tags: the reserved gap is read and discarded,
    # which is cheaper than a second round trip.
    assert cfg.block("sensors").count == 12
    assert len(names) == 8


def test_unknown_tag_raises(cfg):
    with pytest.raises(ConfigError):
        cfg.tag("does_not_exist")


def _write(tmp_path, doc):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _minimal_doc(cfg):
    """A valid minimal config, used as a base for negative tests."""
    return yaml.safe_load(
        textwrap.dedent(
            """
            version: 1
            device: {model: t, host: 0.0.0.0, port: 5020, unit_id: 1}
            enums:
              chamber_state: {0: IDLE, 1: READY, 2: RUNNING, 3: WARM_HOLD,
                              4: STOPPED, 5: FAULT, 6: WAITING, 7: PAUSED}
              alarm_code: {0: NONE, 1: OVERTEMP, 2: SENSOR_FAIL, 3: E_STOP,
                           4: HEATER_FAIL, 5: FAN_FAIL}
              ack_status: {0: ACCEPTED, 1: REJECTED, 2: EXECUTING, 3: COMPLETED,
                           4: FAILED, 5: NONE}
              desired_mode: {0: NONE, 1: WAIT, 2: RUN, 3: WARM}
            command_bits: {0: START, 1: STOP, 2: RESET, 3: ACK_ALARM, 4: WARM,
                           5: PAUSE, 6: RESUME, 7: WAIT, 8: SET}
            blocks:
              status: {function: holding, start: 200, count: 2, access: r}
            holding_registers:
              - {address: 200, name: a, datatype: uint16}
              - {address: 201, name: b, datatype: uint16}
            coils: []
            discrete_inputs: []
            simulation: {}
            """
        )
    )


def test_duplicate_address_rejected(tmp_path, cfg):
    doc = _minimal_doc(cfg)
    doc["holding_registers"][1]["address"] = 200
    with pytest.raises(ConfigError, match="address 200"):
        load_config(_write(tmp_path, doc))


def test_duplicate_name_rejected(tmp_path, cfg):
    doc = _minimal_doc(cfg)
    doc["holding_registers"][1]["name"] = "a"
    with pytest.raises(ConfigError, match="duplicate tag name"):
        load_config(_write(tmp_path, doc))


def test_enum_drift_from_code_rejected(tmp_path, cfg):
    """If the YAML renames a state, the wire protocol would silently lie."""
    doc = _minimal_doc(cfg)
    doc["enums"]["chamber_state"][2] = "GOING"
    with pytest.raises(ConfigError, match="chamber_state"):
        load_config(_write(tmp_path, doc))


def test_bad_scale_rejected(tmp_path, cfg):
    doc = _minimal_doc(cfg)
    doc["holding_registers"][0]["scale"] = 0
    with pytest.raises(ConfigError, match="scale"):
        load_config(_write(tmp_path, doc))


def test_missing_section_rejected(tmp_path, cfg):
    doc = _minimal_doc(cfg)
    del doc["simulation"]
    with pytest.raises(ConfigError, match="missing section"):
        load_config(_write(tmp_path, doc))
