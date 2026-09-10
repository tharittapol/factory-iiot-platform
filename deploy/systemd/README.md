# Bare-metal deployment

For an edge box without a container runtime. Prefer `docker compose` elsewhere.

## Install

```bash
sudo useradd --system --home /opt/plc-sim --shell /usr/sbin/nologin plcsim
sudo mkdir -p /opt/plc-sim/docs /etc/plc-sim

sudo cp -r services/plc-sim/src services/plc-sim/config /opt/plc-sim/
sudo cp services/plc-sim/pyproject.toml services/plc-sim/uv.lock /opt/plc-sim/
sudo cp docs/architecture.md /opt/plc-sim/docs/

cd /opt/plc-sim
sudo uv sync --frozen --no-dev
sudo chown -R plcsim:plcsim /opt/plc-sim
```

## Configure

`/etc/plc-sim/default.env` — shared by every chamber:

```
LOG_LEVEL=INFO
TAGS_FILE=/opt/plc-sim/config/tags.yaml
BIND_HOST=0.0.0.0
```

`/etc/plc-sim/chamber-03.env` — per chamber, overrides the above:

```
BIND_PORT=5022
INJECT_DRIFT=true
```

On bare metal each instance needs its own port, because they share one host IP.
In containers each chamber has its own address and they all use 5020. That
difference is why the container path is the default.

## Run

```bash
sudo cp deploy/systemd/plc-sim@.service deploy/systemd/plc-sim.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plc-sim@chamber-01 plc-sim@chamber-02
```

## Operate

```bash
systemctl status plc-sim@chamber-01
journalctl -u plc-sim@chamber-01 -f
journalctl -u 'plc-sim@*' --since "10 min ago" -p err
systemctl show -p NRestarts plc-sim@chamber-01
systemctl list-units 'plc-sim@*'
```

Check the hardening actually applied:

```bash
systemd-analyze security plc-sim@chamber-01
```

Anything above roughly 5.0 is worth a second look; the directives in the unit
should land it well below that.
