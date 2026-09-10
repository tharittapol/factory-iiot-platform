"""Tests for fault injection.

Each mode is asserted on the property that makes it useful: spike exceeds the
trip point, stuck stops varying, drift stays in range while moving.
"""

from __future__ import annotations

import pytest

from plc_sim.anomaly import AnomalyInjector


@pytest.fixture
def anomalies(cfg):
    return cfg.anomalies


def enable(anomalies, kind, monkeypatch, **overrides):
    cfgcopy = {k: dict(v) for k, v in anomalies.items()}
    cfgcopy[kind].update(overrides)
    monkeypatch.setenv(cfgcopy[kind]["env"], "true")
    return AnomalyInjector(cfgcopy)


def test_disabled_by_default(anomalies):
    inj = AnomalyInjector(anomalies)
    assert inj.active_bitmask == 0
    assert inj.apply("temp_2_x10", 60.0, 100.0) == 60.0


def test_env_var_enables(anomalies, monkeypatch):
    inj = enable(anomalies, "spike", monkeypatch)
    assert "spike" in inj.enabled_kinds
    assert inj.active_bitmask & 0b001


def test_spike_exceeds_trip_point_then_recovers(anomalies, monkeypatch, cfg):
    inj = enable(anomalies, "spike", monkeypatch)
    trip_c = cfg.simulation["safety"]["overtemp_trip_x10"] / 10.0

    inside = inj.apply("temp_2_x10", 70.0, 0.0)
    assert inside > trip_c, "spike must be large enough to trip the alarm"

    period = anomalies["spike"]["period_sec"]
    duration = anomalies["spike"]["duration_sec"]
    outside = inj.apply("temp_2_x10", 70.0, duration + 1)
    assert outside == 70.0

    next_window = inj.apply("temp_2_x10", 70.0, period)
    assert next_window > trip_c, "spike must repeat every period"


def test_spike_only_affects_its_target(anomalies, monkeypatch):
    inj = enable(anomalies, "spike", monkeypatch)
    assert inj.apply("temp_1_x10", 70.0, 0.0) == 70.0


def test_stuck_freezes_after_delay(anomalies, monkeypatch):
    inj = enable(anomalies, "stuck", monkeypatch, after_sec=10)
    assert inj.apply("temp_3_x10", 60.0, 5.0) == 60.0

    frozen = inj.apply("temp_3_x10", 61.0, 11.0)
    assert frozen == 61.0
    # Later reads keep returning the frozen value even as reality changes.
    assert inj.apply("temp_3_x10", 75.0, 60.0) == 61.0
    assert inj.apply("temp_3_x10", 20.0, 900.0) == 61.0


def test_drift_moves_slowly_and_stays_bounded(anomalies, monkeypatch):
    inj = enable(anomalies, "drift", monkeypatch)
    rate = anomalies["drift"]["rate_per_min"]
    cap = anomalies["drift"]["max_offset"]

    assert inj.apply("rh_2_x10", 70.0, 0.0) == 70.0
    after_10_min = inj.apply("rh_2_x10", 70.0, 600.0)
    assert after_10_min == pytest.approx(70.0 + rate * 10, abs=1e-6)

    # A slow drift must never look like a spike, and must plateau.
    assert after_10_min - 70.0 < 1.0
    assert inj.apply("rh_2_x10", 70.0, 10**6) == pytest.approx(70.0 + cap)


def test_multiple_modes_set_distinct_bits(anomalies, monkeypatch):
    for entry in anomalies.values():
        monkeypatch.setenv(entry["env"], "1")
    inj = AnomalyInjector(anomalies)
    assert inj.active_bitmask == 0b111
