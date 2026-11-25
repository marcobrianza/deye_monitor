"""Optional InfluxDB 1.x logging for decoded Deye snapshots."""
import base64
from datetime import datetime
import json
from queue import Empty, Full, Queue
import re
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def field_name(label):
    return re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')


def snapshot_fields(snapshot):
    return {
        field_name(f"{group} {point['name']}"): point['value']
        for group, points in snapshot['groups'].items()
        for point in points
        if point['value'] is not None
    }


class SnapshotPublisher:
    def start_worker(self, destination, label, endpoint):
        self.destination = destination
        self.label = label
        self.status = {'state': 'waiting', 'destination': destination, 'last_write': None}
        print(f'{label} | enabled | {endpoint} | {destination} | waiting for inverter data', flush=True)
        self.pending = Queue(maxsize=1)
        Thread(target=self.run, daemon=True).start()

    def submit(self, snapshot):
        try:
            self.pending.put_nowait(snapshot)
        except Full:
            try:
                self.pending.get_nowait()
            except Empty:
                pass
            self.pending.put_nowait(snapshot)

    def run(self):
        while True:
            snapshot = self.pending.get()
            try:
                written = self.write(snapshot)
            except (OSError, ValueError) as exc:
                message = str(exc)
                self.status = {**self.status, 'state': 'error', 'error': message}
                print(f"{snapshot['timestamp']} | {self.label} | error | {message}", flush=True)
            else:
                if not written:
                    print(f"{snapshot['timestamp']} | {self.label} | skipped: no decoded values", flush=True)
                    continue
                self.status = {'state': 'ok', 'destination': self.destination,
                               'last_write': snapshot['timestamp']}
                print(f"{snapshot['timestamp']} | {self.label} | OK | {self.destination}", flush=True)


class InfluxLogger(SnapshotPublisher):
    def __init__(self, config):
        host = config.get('host', '127.0.0.1')
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        self.url = f"http://{host}:{config.get('port', 8086)}"
        self.database = config.get('database', 'lozza')
        if not re.fullmatch(r'[a-zA-Z0-9_]+', self.database):
            raise ValueError('InfluxDB database name must contain only letters, digits or underscores')
        user, password = config.get('username', ''), config.get('password', '')
        self.headers = {}
        if user or password:
            credentials = base64.b64encode(f'{user}:{password}'.encode()).decode()
            self.headers['Authorization'] = f'Basic {credentials}'
        self.start_worker(self.database, 'InfluxDB', self.url)

    def post(self, path, params, body, content_type):
        request = Request(f'{self.url}/{path}?{urlencode(params)}', data=body,
                          headers={**self.headers, 'Content-Type': content_type})
        with urlopen(request, timeout=3) as response:
            return response.read()

    def write(self, snapshot):
        fields = [f'{name}={float(value)}' for name, value in snapshot_fields(snapshot).items()]
        if not fields:
            return False
        timestamp = round(datetime.fromisoformat(snapshot['timestamp']).timestamp() * 1000)
        data = f"deye {','.join(fields)} {timestamp}".encode()
        params = {'db': self.database, 'precision': 'ms'}
        try:
            self.post('write', params, data, 'text/plain')
        except HTTPError as exc:
            with exc:
                detail = exc.read().decode(errors='replace')
            if exc.code != 404 or 'database not found' not in detail.lower():
                raise
            query = urlencode({'q': f'CREATE DATABASE "{self.database}"'}).encode()
            result = json.loads(self.post('query', {}, query, 'application/x-www-form-urlencoded'))
            for entry in [result, *result.get('results', [])]:
                if 'error' in entry:
                    raise ValueError(entry['error'])
            self.post('write', params, data, 'text/plain')
        return True
