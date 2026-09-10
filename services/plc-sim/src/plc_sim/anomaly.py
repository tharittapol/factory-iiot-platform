"""Fault injection.

Three modes, chosen because they are detectable by different means:

* **spike**  - a short excursion above the trip point. A plain threshold alarm
  catches it. Use it to prove the alarm path works end to end.
* **stuck**  - the reading freezes. No threshold notices, because the frozen
  value is perfectly legal. Only a variance check finds it.
* **drift**  - the reading creeps away slowly and stays in range forever.
  Neither a threshold nor a variance check finds it. This is the one that
  makes statistical anomaly detection worth building.

Injection happens after the physics step and before values reach the registers,
so the controller and any downstream client see exactly what a faulty sensor
would produce.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AnomalySpec:
    kind: str
    target: str
    enabled: bool
    params: dict


class AnomalyInjector:
    """Applies configured anomalies to sensor readings."""

    BIT = {"spike": 0, "stuck": 1, "drift": 2}

    def __init__(self, config: dict) -> None:
        self.specs: list[AnomalySpec] = []
        for kind, entry in (config or {}).items():
            if kind not in self.BIT:
                continue
            env_name = entry.get("env", f"INJECT_{kind.upper()}")
            enabled = _env_flag(env_name, bool(entry.get("enabled", False)))
            self.specs.append(
                AnomalySpec(
                    kind=kind,
                    target=entry.get("target", ""),
                    enabled=enabled,
                    params=entry,
                )
            )
        self._stuck_values: dict[str, float] = {}

    @property
    def active_bitmask(self) -> int:
        mask = 0
        for spec in self.specs:
            if spec.enabled:
                mask |= 1 << self.BIT[spec.kind]
        return mask

    @property
    def enabled_kinds(self) -> list[str]:
        return [s.kind for s in self.specs if s.enabled]

    def apply(self, tag_name: str, value: float, elapsed_sec: float) -> float:
        for spec in self.specs:
            if not spec.enabled or spec.target != tag_name:
                continue
            value = self._apply_one(spec, tag_name, value, elapsed_sec)
        return value

    def _apply_one(
        self, spec: AnomalySpec, tag_name: str, value: float, elapsed: float
    ) -> float:
        p = spec.params

        if spec.kind == "spike":
            period = float(p.get("period_sec", 600))
            duration = float(p.get("duration_sec", 12))
            if period > 0 and (elapsed % period) < duration:
                return value + float(p.get("magnitude_c", 25.0))
            return value

        if spec.kind == "stuck":
            after = float(p.get("after_sec", 300))
            if elapsed < after:
                return value
            # Freeze at whatever the value was when the fault began.
            return self._stuck_values.setdefault(tag_name, value)

        if spec.kind == "drift":
            rate_per_min = float(p.get("rate_per_min", 0.05))
            max_offset = float(p.get("max_offset", 8.0))
            offset = min(max_offset, rate_per_min * (elapsed / 60.0))
            return value + offset

        return value
