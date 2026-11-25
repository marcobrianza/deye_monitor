#!/bin/sh
set -eu

cd "$(dirname "$0")"
target=/opt/deye-monitor
service=deye-monitor.service

if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this installer as root.' >&2
    exit 1
fi
if [ ! -f "$target/config.json" ] && [ ! -f config.json ]; then
    echo 'Place your config.json next to this script before running it.' >&2
    exit 1
fi
if ! id deye-monitor >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$target" --shell /usr/sbin/nologin deye-monitor
fi
install -d -m 0755 "$target"
if [ ! -x "$target/.venv/bin/pip" ]; then
    if ! python3 -m venv "$target/.venv"; then
        apt-get update
        apt-get install -y python3-venv
        python3 -m venv "$target/.venv"
    fi
fi
"$target/.venv/bin/python" -m pip install -r requirements.txt

install -m 0644 deye_monitor.py collector.py modbus_transport.py mqtt_logger.py \
    influx_logger.py monitor.html registers.json requirements.txt config.default.json "$target/"
if [ ! -f "$target/config.json" ]; then
    install -o root -g deye-monitor -m 0640 config.json "$target/config.json"
fi
chown root:deye-monitor "$target/config.json"
chmod 0640 "$target/config.json"

install -m 0644 "$service" "/etc/systemd/system/$service"
systemd-analyze verify "/etc/systemd/system/$service"
systemctl daemon-reload
systemctl enable "$service"
systemctl restart "$service"
systemctl is-active "$service"
