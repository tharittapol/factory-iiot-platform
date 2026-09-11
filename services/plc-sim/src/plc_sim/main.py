"""Entry point: wire the modules together and run the scan loop.

One process serves one chamber on one TCP endpoint, mirroring real hardware
where each PLC owns an address. Four chambers means four containers, so adding
a fifth is a compose entry rather than a code change.

Tick order matters and follows what a real PLC scan does:

    read gateway heartbeat -> decide link state
    advance own heartbeat
    read command frame, but act only if cmd_seq changed
    run the state machine
    step the physics
    sample sensors, inject anomalies
    check safety
    publish status, sensors and diagnostics
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

from .anomaly import AnomalyInjector
from .config import Config, load_config
from .constants import ChamberState, next_counter
from .modbus_map import ModbusMap
from .physics import ChamberPhysics
from .state_machine import ChamberController, CommandFrame

log = logging.getLogger("plc_sim")


class ChamberSimulator:
    def __init__(self, cfg: Config, chamber_id: str, host: str, port: int) -> None:
        self.cfg = cfg
        self.chamber_id = chamber_id
        sim = cfg.simulation

        self.physics = ChamberPhysics(sim, seed=chamber_id)
        self.controller = ChamberController(params=sim)
        self.anomaly = AnomalyInjector(cfg.anomalies)
        self.mb = ModbusMap(cfg, host=host, port=port)

        self.tick_interval = float(sim.get("tick_interval_sec", 0.2))
        hb = sim.get("heartbeat", {})
        self.plc_hb_period = float(hb.get("plc_period_sec", 1.0))
        self.gw_timeout = float(hb.get("gw_timeout_sec", 30.0))

        self.temp_tags = [
            t for t in cfg.tags_by_function("holding") if t.sim.get("source") == "chamber_temp"
        ]
        self.rh_tags = [
            t for t in cfg.tags_by_function("holding") if t.sim.get("source") == "chamber_humidity"
        ]
        self.sensors = {
            t.name: self.physics.make_sensor(t.name, t.sim) for t in self.temp_tags + self.rh_tags
        }

        self.started_at = time.monotonic()
        self.plc_hb = 0
        self._plc_hb_elapsed = 0.0
        self._last_gw_hb = 0
        self._gw_hb_seen_at = time.monotonic()
        self._last_tick = time.monotonic()
        self.request_count = 0

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        server = self.mb.build_server()
        await self._seed_registers()

        log.info(
            "chamber=%s listening on %s:%d unit_id=%d anomalies=%s",
            self.chamber_id,
            self.mb.host,
            self.mb.port,
            self.mb.device_id,
            ",".join(self.anomaly.enabled_kinds) or "none",
        )

        serve = asyncio.create_task(server.serve_forever())
        loop = asyncio.create_task(self._scan_loop())
        try:
            await asyncio.gather(serve, loop)
        except asyncio.CancelledError:
            pass
        finally:
            await server.shutdown()

    async def _seed_registers(self) -> None:
        await self.mb.write_tag("config_version", self.cfg.version)
        await self.mb.write_tag("setpoint_x10", self.controller.setpoint_c)
        await self.mb.write_tag("warm_hold_x10", self.controller.warm_hold_c)
        await self._publish()

    async def _scan_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except Exception:  # keep the server alive; a dead PLC is worse
                log.exception("tick failed on chamber=%s", self.chamber_id)
            elapsed = time.monotonic() - started
            await self.mb.write_tag("scan_time_ms", int(elapsed * 1000))
            await asyncio.sleep(max(0.01, self.tick_interval - elapsed))

    # -- one scan -----------------------------------------------------------

    async def tick(self) -> None:
        now = time.monotonic()
        dt = max(0.05, min(1.0, now - self._last_tick))
        self._last_tick = now

        await self._update_link(now)
        self._advance_heartbeat(dt)
        await self._maybe_apply_command()

        temps_c, rh_pct = self._sample_sensors(now)
        self.controller.step(dt, temps_c)
        self.physics.step(
            dt,
            heater_on=self.controller.heater_on,
            fan_on=self.controller.fan_on,
            target_c=self.controller.target_c,
        )
        await self._publish(temps_c, rh_pct)

    async def _update_link(self, now: float) -> None:
        gw_hb = int(await self.mb.read_tag("gw_heartbeat"))
        if gw_hb != self._last_gw_hb:
            self._last_gw_hb = gw_hb
            self._gw_hb_seen_at = now
        self.controller.gw_link_ok = (now - self._gw_hb_seen_at) <= self.gw_timeout

    def _advance_heartbeat(self, dt: float) -> None:
        self._plc_hb_elapsed += dt
        while self._plc_hb_elapsed >= self.plc_hb_period:
            self._plc_hb_elapsed -= self.plc_hb_period
            self.plc_hb = next_counter(self.plc_hb)

    async def _maybe_apply_command(self) -> None:
        seq = int(await self.mb.read_tag("cmd_seq"))
        if seq == 0 or seq == self.controller.last_seq_processed:
            return  # nothing committed since last scan

        frame = CommandFrame(
            cmd_word=int(await self.mb.read_tag("cmd_word")),
            setpoint_c=float(await self.mb.read_tag("setpoint_x10")),
            warm_hold_c=float(await self.mb.read_tag("warm_hold_x10")),
            step_index=int(await self.mb.read_tag("step_index")),
            profile_len=int(await self.mb.read_tag("profile_len")),
            desired_mode=int(await self.mb.read_tag("desired_mode")),
            seq=seq,
        )
        self.controller.apply_command(frame)
        self.request_count += 1
        log.debug(
            "chamber=%s applied cmd seq=%d word=0x%x -> %s",
            self.chamber_id,
            seq,
            frame.cmd_word,
            self.controller.state.name,
        )

    def _sample_sensors(self, now: float) -> tuple[list[float], list[float]]:
        elapsed = now - self.started_at
        temps, rh = [], []
        for tag in self.temp_tags:
            v = self.physics.read_sensor(self.sensors[tag.name], "chamber_temp")
            temps.append(self.anomaly.apply(tag.name, v, elapsed))
        for tag in self.rh_tags:
            v = self.physics.read_sensor(self.sensors[tag.name], "chamber_humidity")
            rh.append(self.anomaly.apply(tag.name, v, elapsed))
        return temps, rh

    # -- publishing ---------------------------------------------------------

    async def _publish(
        self, temps_c: list[float] | None = None, rh_pct: list[float] | None = None
    ) -> None:
        c = self.controller

        if temps_c is not None:
            for tag, value in zip(self.temp_tags, temps_c, strict=True):
                await self.mb.write_tag(tag.name, value)
            await self.mb.write_tag("temp_max_x10", max(temps_c))
        if rh_pct is not None:
            for tag, value in zip(self.rh_tags, rh_pct, strict=True):
                await self.mb.write_tag(tag.name, value)
            await self.mb.write_tag("rh_avg_x10", sum(rh_pct) / len(rh_pct))

        await self.mb.write_many(
            {
                "chamber_state": int(c.state),
                "alarm_code": int(c.alarm),
                "heater_on": int(c.heater_on),
                "setpoint_latched_x10": c.setpoint_c,
                "step_index_latched": c.step_index,
                "gw_link_ok": int(c.gw_link_ok),
                "plc_heartbeat": self.plc_hb,
                "ack_status": int(c.ack),
                "last_cmd_seq_done": c.last_seq_done,
                "state_seq": c.state_seq,
            }
        )

        await self.mb.write_many(
            {
                "alarm_active": c.alarm_active,
                "heater_feedback": c.heater_on,
                "fan_feedback": c.fan_on,
                "estop_ok": c.estop_ok,
                "door_closed": c.door_closed,
                "sensor_1_ok": c.sensors_ok[0],
                "sensor_2_ok": c.sensors_ok[1],
                "sensor_3_ok": c.sensors_ok[2],
                "sensor_4_ok": c.sensors_ok[3],
                "gw_link_ok_di": c.gw_link_ok,
                "running": c.state == ChamberState.RUNNING,
            }
        )

        await self.mb.write_many(
            {
                "uptime_sec": int(time.monotonic() - self.started_at),
                "request_count": self.request_count,
                "sim_anomaly_active": self.anomaly.active_bitmask,
            }
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line beats environment beats tags.yaml.

    argparse is here so that an unrecognised flag stops the process. Silently
    ignoring input the operator meant is the same class of failure as a
    directive in the wrong section of a unit file: it looks like it worked.
    """
    default_tags = Path(__file__).resolve().parents[2] / "config" / "tags.yaml"

    parser = argparse.ArgumentParser(
        prog="plc-sim",
        description="Modbus TCP simulator for one drying chamber.",
    )
    parser.add_argument(
        "--chamber-id",
        default=os.getenv("CHAMBER_ID", "chamber-01"),
        help="identifies the chamber and seeds its model (env: CHAMBER_ID)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("BIND_HOST"),
        help="address to bind (env: BIND_HOST, else tags.yaml)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("BIND_PORT")) if os.getenv("BIND_PORT") else None,
        help="port to bind (env: BIND_PORT, else tags.yaml)",
    )
    parser.add_argument(
        "--tags",
        default=os.getenv("TAGS_FILE", str(default_tags)),
        help="path to tags.yaml (env: TAGS_FILE)",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="DEBUG, INFO, WARNING, ERROR (env: LOG_LEVEL)",
    )
    return parser.parse_args(argv)


def build_simulator(args: argparse.Namespace | None = None) -> ChamberSimulator:
    args = args or parse_args([])
    cfg = load_config(args.tags)
    host = args.host or cfg.device.host
    port = args.port or cfg.device.port
    return ChamberSimulator(cfg, args.chamber_id, host, port)


async def _amain(args: argparse.Namespace) -> None:
    sim = build_simulator(args)
    task = asyncio.create_task(sim.run())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        log.info("shutting down")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
