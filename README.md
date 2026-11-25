# Deye Monitor

Part of a home monitoring setup built around three independent Python programs. Deye Monitor reads a solar inverter, Gree Monitor reads and controls a heat pump, and MQTT Monitor collects readings published by devices such as Shelly. Each has a local web dashboard and its own Python environment.

Companion projects: [Gree Monitor](https://github.com/marcobrianza/gree_monitor) · [MQTT Monitor](https://github.com/marcobrianza/mqtt_monitor).

## Host and dependencies

- Python 3.10+ and the packages listed in `requirements.txt`. Run on macOS or Linux for local use and testing. On Raspberry Pi with a 64-bit Debian-based OS, the included systemd unit supports unattended operation.
- An MQTT broker such as Mosquitto 2.x, normally on TCP 1883. The broker is required for MQTT Monitor, and is optional for Deye and Gree when MQTT publication is disabled.
- InfluxDB **1.x** on TCP 8086 for historical storage. The examples target its v1 HTTP API and assume the configured database already exists. Switching off InfluxDB logging leaves the live dashboard available. Grafana is optional and can query InfluxDB using InfluxQL.
- Example configurations preserve private LAN addresses from the original setup. Set your Modbus, MQTT and InfluxDB endpoints before starting on a different network. Actual credentials belong in local files ignored by Git.

Python dashboard and diagnostic collector for the SUN-10K-SG05LP3-EU-SM2.

## Setup

For a fresh installation, copy the template once:

```sh
cp -n config.default.json config.json
```

Edit `config.json` with your inverter address and credentials. It is the only
configuration file read at runtime and is ignored by Git. Commit
`config.default.json`, with empty credential fields. If `config.json` was
already tracked, run `git rm --cached -- config.json` from this folder before
committing. Keep your existing `config.json` when updating the monitor.

If the dependencies are missing, run these commands from the extracted folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use `.venv/bin/python` in place of `python3` when running inside the virtual environment.

## Web monitor

```sh
python3 deye_monitor.py
```

Open <http://127.0.0.1:8766/> on the host computer. The process reads the
inverter at startup and every 60 seconds. Press Ctrl+C to stop it.

- Set `location_label` in `config.json` to show a site name under the dashboard title. Leave it empty to hide it.
- `config.json` contains all settings. `poll_interval_seconds` controls how often the monitor runs a read. `block_size` limits the number of registers per Modbus request. Set `modbus_enabled` to `false` to disable Modbus polling and writes.
- `registers.json` defines the groups, register addresses, scales, signed values and units. Register pairs list the low word first. `deye_monitor.py` uses this map to select and decode the registers. The diagnostic collector uses `ranges` in `config.json` for raw scans.
- The page refreshes automatically and shows the last complete sample when a read fails, if one is available. The monitor keeps data in memory.
- In **Battery reserve · Time of Use**, choose **Edit** for a slot, adjust the minimum SOC and choose **Save to inverter**. The monitor writes that slot's minimum SOC and reads it back for verification.
- Enable **Register addresses and raw values** to compare readings with the Modbus map. Verify the power direction and the interpretation of GEN and AC Solar against the inverter display.

Run `deye_monitor.py` for the dashboard and continuous MQTT/InfluxDB telemetry.
The terminal prints each destination at startup, and reports successful,
failed or skipped deliveries for each poll. The diagnostic `collector.py`
only scans registers and writes its local dump.

Restart the monitor after editing its configuration or register map.

When upgrading from the two-file layout, copy your monitor settings and
MQTT/InfluxDB settings into the new `config.json` and run
`python3 deye_monitor.py`. The old `monitor.json` is no longer used.

## Raspberry Pi service (DietPi/Debian)

Transfer the ZIP and your private `config.json` over SCP. On the Raspberry Pi,
extract the ZIP, place `config.json` next to `install-raspberry.sh`, and
run `sh install-raspberry.sh` as root. The installer uses
`/opt/deye-monitor`, a dedicated system account and a Python virtual
environment. It preserves an existing configuration in the install directory.

The service starts at boot and restarts after a process exit. To inspect it:

```sh
systemctl status deye-monitor
journalctl -u deye-monitor -f
```

With the default localhost web listener, open an SSH tunnel from your computer
(`ssh -N -L 8766:127.0.0.1:8766 root@192.168.13.13`) and open
<http://127.0.0.1:8766/>. Use another local port to view the dashboard if your
Mac already uses 8766, or stop the Mac monitor before switching to the Pi.

## InfluxDB logging

Edit the `influxdb` section of `config.json` and restart the monitor.
To enable logging to InfluxDB 1.x, set `logging_enabled` to `true`. The defaults point to
`127.0.0.1:8086`, using database `lozza`. Set `logging_enabled` to
`false` on a computer that should only show the dashboard. Leave
`username` and `password` empty for a server without authentication.

Each poll writes the available decoded values, including energy counters,
settings and alarms, to measurement `deye`. Fields use the group and point
names in lowercase with underscores (for example `battery_soc` and
`energy_pv_generation_today`). Absent register values are omitted.
If the database is missing, the monitor tries to create it. With
authentication enabled, use an account with write permission and either
create the database in advance or grant permission to create it.

The dashboard shows InfluxDB status and the timestamp of the last saved
sample. Green indicates a successful write, red shows the write error, and
gray indicates disabled logging or a wait for data.

The dashboard and Modbus polling continue during an InfluxDB outage.
Failed samples are skipped and the logger retries on subsequent polls.

## MQTT telemetry

MQTT publication is enabled by default. Edit the `mqtt` section of
`config.json` and enter the broker username and password before running
the monitor. The default destination is `192.168.13.13:1883`, topic
`lozza/deye`. MQTT and InfluxDB can be switched on independently.

Each poll with decoded values sends one JSON message, with QoS 1 and no
retain flag. It contains the sample timestamp, measurement name and the
same decoded field names and values used by the InfluxDB logger:

```json
{"timestamp":"2026-11-24T19:00:00.000+01:00","measurement":"deye","fields":{"battery_soc":75}}
```

The MQTT LED turns green after an acknowledgement from the broker. Connection
or publication errors are shown in red. The monitor tries again on subsequent
polls.

## Diagnostic collector

```sh
python3 collector.py --probe
python3 collector.py
```

The probe prints register 0. A full run reads the configured address ranges
using FC03 and appends one JSON object to `deye_dump.jsonl`, including raw
unsigned 16-bit values, rejected addresses and any communication error.
Use `--range FIRST LAST` to read a specific inclusive address range.

The default is Modbus TCP port 502 with unit ID 1. Use `--port 8899` if the
endpoint provides standard Modbus TCP on that port. Solarman V5 uses
different framing.

Exit code 2 indicates an empty or interrupted dump. Exit code 3 indicates
that another run is using the same output file.

## Register references

- [Deye three-phase protocol V105.4](https://github.com/davidrapan/ha-solarman/wiki/Documentation#deye)
- [Deye three-phase protocol PDF](https://download.axilogi.com/DEYE/Other/MODBUS_RTU.pdf)
- [Three-phase hybrid register map in YAML](https://github.com/Developer089/deye-modbus-ha/blob/main/custom_components/deye_modbus/maps/sun_3ph_hybrid.yaml)
- [SG05LP3-EU-SM2 register map in TSV](https://github.com/mxbode/Deye-SUN-SG05LP3-EU-SM2-Modbus-TSV)
