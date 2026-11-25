#!/usr/bin/env python3
"""Serve the latest Deye Modbus snapshot from memory."""
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
import json
import math
import sys
import time

from collector import scan
from influx_logger import InfluxLogger
from modbus_transport import TCPInstrument, mm

BASE = Path(__file__).resolve().parent


def register_ranges(groups):
    addresses = sorted({a for points in groups.values() for p in points for a in p['addresses']})
    ranges = []
    for address in addresses:
        if ranges and address == ranges[-1][1] + 1:
            ranges[-1][1] = address
        else:
            ranges.append([address, address])
    return ranges


def decode(point, raw):
    addresses = point['addresses']
    if any(a not in raw for a in addresses):
        return None
    number = sum(raw[a] << (16 * i) for i, a in enumerate(addresses))
    bits = 16 * len(addresses)
    if point.get('signed') and number >= 1 << (bits - 1):
        number -= 1 << bits
    return round((number + point.get('offset', 0)) * point.get('scale', 1), 6)


def acquire(instrument, ranges, groups, pause, block_size):
    raw, rejected, error = scan(instrument, ranges, block_size, pause)
    return {
        'timestamp': datetime.now().astimezone().isoformat(timespec='milliseconds'),
        'read_count': len(raw),
        'error': error,
        'rejected': rejected,
        'groups': {
            name: [{**p, 'value': decode(p, raw), 'raw': [raw.get(a) for a in p['addresses']]}
                   for p in points]
            for name, points in groups.items()
        },
    }


class ReserveChanged(Exception):
    pass


def set_reserve(instrument, slot, expected, value, pause):
    if (type(slot) is not int or not 1 <= slot <= 6
            or type(expected) is not int or not 0 <= expected <= 100
            or type(value) is not int or not 0 <= value <= 100):
        raise ValueError('Invalid slot or percentage')
    address = 165 + slot
    current = instrument.read_registers(address, 1, functioncode=3)[0]
    if current != expected:
        raise ReserveChanged(f'The inverter now reports {current}%. Wait for the next refresh.')
    if current == value:
        return current
    time.sleep(pause)
    try:
        instrument.write_registers(address, [value])
    except OSError:
        # An acknowledgement can be lost after the inverter applied the write.
        pass
    time.sleep(pause)
    return instrument.read_registers(address, 1, functioncode=3)[0]


def main():
    config_path = BASE / 'config.json'
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise SystemExit(
            f'Missing {config_path}. Copy config.default.json to config.json and edit it before starting.'
        ) from None
    publishers = {}
    publisher_status = {}
    print(f'Deye Monitor | configuration: {config_path}', flush=True)
    for name, label in (('influxdb', 'InfluxDB'), ('mqtt', 'MQTT')):
        settings = config.get(name)
        publisher_status[name] = {'state': 'disabled'}
        if not isinstance(settings, dict) or settings.get('logging_enabled') is not True:
            print(f'{label} | disabled', flush=True)
            continue
        try:
            if name == 'mqtt':
                from mqtt_logger import MQTTLogger
                publishers[name] = MQTTLogger(settings)
            else:
                publishers[name] = InfluxLogger(settings)
        except (ImportError, TypeError, ValueError) as exc:
            publisher_status[name] = {'state': 'error', 'error': str(exc)}
            print(f'{label} | configuration error: {exc}', flush=True)
    groups = json.loads((BASE / 'registers.json').read_text())
    ranges = register_ranges(groups)
    modbus_enabled = config.get('modbus_enabled', True)
    interval = config['poll_interval_seconds']
    timeout = config['timeout_seconds']
    pause = config['request_pause_seconds']
    block_size = config['block_size']
    if not (math.isfinite(interval) and interval > 0 and math.isfinite(timeout)
            and timeout > 0 and math.isfinite(pause) and pause >= 0):
        raise ValueError('Check the interval, timeout and request delay in config.json')
    if type(block_size) is not int or not 1 <= block_size <= 125:
        raise ValueError('block_size in config.json must be an integer from 1 to 125')
    instrument = TCPInstrument(config['host'], config['port'], config['unit_id'], timeout)
    location = config.get('location_label', '').strip()
    location_html = f'<p>{escape(location)}</p>' if location else ''
    html = (BASE / 'monitor.html').read_text(encoding='utf-8')
    model = escape(config.get('model', ''))
    html = html.replace('<!-- inverter-model -->', model)
    html = html.replace('<!-- location-label -->', location_html).encode('utf-8')
    stop = Event()
    refresh_requested = Event()
    modbus_lock = Lock()
    port = config['web_port']
    local_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    snapshot = {'interval': interval, 'attempt': None, 'last_good': None,
                'reading': False, 'next_read_at': None}

    def reading_ticks(done):
        while not done.wait(1):
            print('.', end='', flush=True)

    def poll():
        nonlocal snapshot
        while not stop.is_set():
            start = time.monotonic()
            refresh_requested.clear()
            with modbus_lock:
                snapshot = {**snapshot, 'reading': True, 'next_read_at': None}
                print('Reading inverter registers', end='' if sys.stdout.isatty() else '\n', flush=True)
                done = Event()
                ticker = Thread(target=reading_ticks, args=(done,), daemon=True) if sys.stdout.isatty() else None
                if ticker:
                    ticker.start()
                try:
                    result = acquire(instrument, ranges, groups, pause, block_size)
                finally:
                    done.set()
                    if ticker:
                        ticker.join()
                        print(flush=True)
            last_good = snapshot['last_good']
            if result['read_count'] and not result['error'] and not result['rejected']:
                last_good = result
            duration = time.monotonic() - start
            delay = interval - duration if duration < interval else interval
            deadline = time.monotonic() + delay
            next_read_at = time.time() + delay
            snapshot = {'interval': interval, 'attempt': result, 'last_good': last_good,
                        'reading': False, 'next_read_at': next_read_at}
            for publisher in publishers.values():
                publisher.submit(result)
            rejected = len(result['rejected'])
            print(f"{result['timestamp']} | {result['read_count']} registers read in {duration:.1f} s | "
                  f"{rejected} rejected | {result['error'] or 'read complete'}", flush=True)
            next_time = datetime.fromtimestamp(next_read_at).strftime('%H:%M:%S')
            print(f'Waiting for next reading at {next_time}', flush=True)
            refresh_requested.wait(max(0, deadline - time.monotonic()))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                body, content_type = html, 'text/html; charset=utf-8'
            elif self.path == '/api/snapshot':
                statuses = {name: publisher.status for name, publisher in publishers.items()}
                data = {**snapshot, 'modbus_enabled': modbus_enabled, **publisher_status, **statuses}
                body = json.dumps(data, ensure_ascii=False).encode()
                content_type = 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != '/api/reserve':
                self.send_error(404)
                return
            if not modbus_enabled:
                self.send_json(503, {'error': 'Modbus polling is disabled.'})
                return
            host = self.headers.get('Host')
            if (self.client_address[0] != '127.0.0.1' or host not in local_hosts
                    or self.headers.get('Origin') != f'http://{host}'):
                self.send_json(403, {'error': 'Open the monitor from a browser on the host computer.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 256 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Invalid request')
                payload = json.loads(self.rfile.read(length))
                slot, expected, value = (payload[key] for key in ('slot', 'expected', 'value'))
                with modbus_lock:
                    actual = set_reserve(instrument, slot, expected, value, pause)
                if actual != value:
                    self.send_json(409, {'error': f'Requested {value}%, read back {actual}%.'})
                else:
                    self.send_json(200, {'value': actual})
            except (ValueError, KeyError, TypeError) as exc:
                self.send_json(400, {'error': str(exc)})
            except ReserveChanged as exc:
                self.send_json(409, {'error': str(exc)})
            except (OSError, mm.SlaveReportedException) as exc:
                self.send_json(502, {'error': f'Save not confirmed by the inverter: {exc}'})
            finally:
                refresh_requested.set()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer((config['web_host'], config['web_port']), Handler) as server:
        if modbus_enabled:
            Thread(target=poll, daemon=True).start()
        print(f"http://{config['web_host']}:{config['web_port']}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            refresh_requested.set()


if __name__ == '__main__':
    main()
