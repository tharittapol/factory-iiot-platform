"""Thermal and moisture model for one drying chamber.

Deliberately knows nothing about Modbus, registers or addresses: numbers in,
numbers out. That is what makes it testable without opening a socket.

The model is intentionally simple but not random: temperature responds to the
heater with a gap-dependent gain, loses heat proportional to the difference
from ambient, and humidity follows the moisture left in the load. Sensors then
observe this one chamber through their own fixed gradient and noise, which is
why four sensors move together instead of wandering independently.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class SensorProfile:
    """How one physical sensor observes the chamber."""

    name: str
    gradient: float = 0.0  # fixed positional bias, in engineering units
    offset: float = 0.0  # per-unit calibration error, drawn once at startup
    noise: float = 0.0  # +/- amplitude of per-read noise


class ChamberPhysics:
    """One chamber's thermal and moisture state."""

    def __init__(self, params: dict, seed: str = "chamber") -> None:
        self.rng = random.Random(seed)

        ambient = params.get("ambient", {})
        thermal = params.get("thermal", {})
        moisture = params.get("moisture", {})

        self.ambient_temp_c: float = float(ambient.get("temp_c", 31.0))
        self.ambient_humidity_pct: float = float(ambient.get("humidity_pct", 72.0))

        self.heat_gain_per_sec: float = float(thermal.get("heat_gain_per_sec", 0.26))
        self.passive_cool_per_sec: float = float(thermal.get("passive_cool_per_sec", 0.018))
        self.fan_cool_per_sec: float = float(thermal.get("fan_cool_per_sec", 0.018))
        self.max_temp_c: float = float(thermal.get("max_temp_c", 102.0))

        self.drying_rate_per_sec: float = float(moisture.get("drying_rate_per_sec", 0.0022))
        self.wet_recovery_per_sec: float = float(moisture.get("wet_recovery_per_sec", 0.00015))

        # Starting conditions vary a little per chamber so a four-chamber
        # dashboard does not show four identical lines.
        self.temp_c: float = self.ambient_temp_c + self.rng.uniform(-1.0, 1.5)
        initial_moisture = float(moisture.get("initial_ratio", 0.78))
        self.moisture_ratio: float = initial_moisture + self.rng.uniform(-0.08, 0.10)
        self.load_mass_ratio: float = 0.9 + self.rng.uniform(-0.15, 0.15)

    # -- main step ----------------------------------------------------------

    def step(self, dt: float, heater_on: bool, fan_on: bool, target_c: float) -> None:
        """Advance the model by ``dt`` seconds."""
        if dt <= 0:
            return

        ambient = self.ambient_temp_c + self.rng.uniform(-0.05, 0.05)

        if heater_on and target_c > 0:
            # Gain tapers as the chamber approaches the setpoint, which is what
            # stops the temperature from overshooting on every cycle.
            gap_factor = max(0.25, min(1.5, (target_c - self.temp_c + 20.0) / 35.0))
            heat_gain = self.heat_gain_per_sec * gap_factor * dt
        else:
            heat_gain = 0.0

        passive_loss = (self.temp_c - ambient) * self.passive_cool_per_sec * dt
        fan_loss = self.fan_cool_per_sec * dt if fan_on else 0.0

        self.temp_c += heat_gain - passive_loss - fan_loss
        self.temp_c = max(ambient - 1.5, min(self.max_temp_c, self.temp_c))

        self._step_moisture(dt, heater_on, ambient)

    def _step_moisture(self, dt: float, heater_on: bool, ambient: float) -> None:
        drying_force = max(0.0, self.temp_c - ambient) / 40.0
        if heater_on:
            self.moisture_ratio -= (
                self.drying_rate_per_sec * drying_force * self.load_mass_ratio * dt
            )
        else:
            # A stopped load slowly reabsorbs moisture from the room.
            self.moisture_ratio += self.wet_recovery_per_sec * dt
        self.moisture_ratio = max(0.05, min(1.0, self.moisture_ratio))

    # -- derived values -----------------------------------------------------

    @property
    def humidity_pct(self) -> float:
        """Chamber humidity, driven by remaining moisture and temperature."""
        value = (
            self.ambient_humidity_pct * 0.45
            + self.moisture_ratio * 42.0
            - max(0.0, self.temp_c - self.ambient_temp_c) * 0.12
        )
        return max(12.0, min(98.0, value))

    # -- sensor observation -------------------------------------------------

    def make_sensor(self, name: str, sim: dict) -> SensorProfile:
        """Build a sensor profile from a tag's ``sim`` block.

        ``offset_range`` is sampled once here, not per read: a real sensor has a
        persistent calibration error, not a fresh one every scan.
        """
        lo, hi = sim.get("offset_range", [0.0, 0.0])
        return SensorProfile(
            name=name,
            gradient=float(sim.get("gradient", 0.0)),
            offset=self.rng.uniform(float(lo), float(hi)),
            noise=float(sim.get("noise", 0.0)),
        )

    def read_sensor(self, profile: SensorProfile, source: str) -> float:
        """Observe the chamber through one sensor, in engineering units."""
        if source == "chamber_temp":
            base = self.temp_c
        elif source == "chamber_humidity":
            base = self.humidity_pct
        else:
            raise ValueError(f"unknown sim source: {source!r}")

        noise = self.rng.uniform(-profile.noise, profile.noise) if profile.noise else 0.0
        return base + profile.gradient + profile.offset + noise