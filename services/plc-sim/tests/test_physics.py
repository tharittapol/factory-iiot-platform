"""Tests for the thermal model. No sockets, no registers."""

from __future__ import annotations

import pytest

from plc_sim.physics import ChamberPhysics


def run(physics, seconds, heater_on, fan_on=False, target_c=70.0, dt=1.0):
    for _ in range(int(seconds / dt)):
        physics.step(dt, heater_on=heater_on, fan_on=fan_on, target_c=target_c)


def test_starts_near_ambient(cfg):
    p = ChamberPhysics(cfg.simulation, seed="t")
    assert abs(p.temp_c - p.ambient_temp_c) < 2.0


def test_heater_raises_temperature(cfg):
    p = ChamberPhysics(cfg.simulation, seed="t")
    before = p.temp_c
    run(p, 60, heater_on=True)
    assert p.temp_c > before + 5.0


def test_cools_toward_ambient_when_idle(cfg):
    p = ChamberPhysics(cfg.simulation, seed="t")
    run(p, 600, heater_on=True)
    hot = p.temp_c
    run(p, 1800, heater_on=False, target_c=0.0)
    assert p.temp_c < hot
    assert p.temp_c == pytest.approx(p.ambient_temp_c, abs=3.0)


def test_never_exceeds_max_temp(cfg):
    p = ChamberPhysics(cfg.simulation, seed="t")
    run(p, 20000, heater_on=True, target_c=200.0)
    assert p.temp_c <= p.max_temp_c


def test_drying_reduces_moisture_and_humidity(cfg):
    p = ChamberPhysics(cfg.simulation, seed="t")
    start_moisture = p.moisture_ratio
    run(p, 3600, heater_on=True)
    assert p.moisture_ratio < start_moisture
    assert 12.0 <= p.humidity_pct <= 98.0


def test_same_seed_is_reproducible(cfg):
    a = ChamberPhysics(cfg.simulation, seed="chamber-01")
    b = ChamberPhysics(cfg.simulation, seed="chamber-01")
    assert a.temp_c == b.temp_c
    run(a, 60, heater_on=True)
    run(b, 60, heater_on=True)
    assert a.temp_c == b.temp_c


def test_different_seed_diverges(cfg):
    a = ChamberPhysics(cfg.simulation, seed="chamber-01")
    b = ChamberPhysics(cfg.simulation, seed="chamber-02")
    assert a.temp_c != b.temp_c


def test_sensors_track_one_chamber(cfg):
    """Four sensors must move together, not wander independently."""
    p = ChamberPhysics(cfg.simulation, seed="t")
    tags = [t for t in cfg.tags_by_function("holding")
            if t.sim.get("source") == "chamber_temp"]
    profiles = [p.make_sensor(t.name, t.sim) for t in tags]

    cold = [p.read_sensor(pr, "chamber_temp") for pr in profiles]
    run(p, 300, heater_on=True)
    hot = [p.read_sensor(pr, "chamber_temp") for pr in profiles]

    assert all(h > c for h, c in zip(hot, cold, strict=True))
    assert max(hot) - min(hot) < 5.0  # same chamber, small spread


def test_sensor_offset_is_persistent(cfg):
    """A calibration error stays put; only noise varies per read."""
    p = ChamberPhysics(cfg.simulation, seed="t")
    tag = next(t for t in cfg.tags_by_function("holding")
               if t.sim.get("source") == "chamber_temp")
    profile = p.make_sensor(tag.name, tag.sim)
    reads = [p.read_sensor(profile, "chamber_temp") for _ in range(20)]
    spread = max(reads) - min(reads)
    assert spread <= 2 * profile.noise + 1e-6