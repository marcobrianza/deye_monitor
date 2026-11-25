"""Publish decoded Deye readings as JSON over MQTT."""
import json
from time import monotonic

from paho.mqtt import client as mqtt

from influx_logger import SnapshotPublisher, snapshot_fields


class MQTTLogger(SnapshotPublisher):
    def __init__(self, config):
        self.host = config.get('host', '192.168.13.13')
        self.port = int(config.get('port', 1883))
        self.topic = config.get('topic', 'lozza/deye')
        self.username = config.get('username', '')
        self.password = config.get('password', '')
        if not 1 <= self.port <= 65535:
            raise ValueError('Invalid MQTT port')
        if not self.topic or any(char in self.topic for char in '+#\0'):
            raise ValueError('Invalid MQTT topic')
        if not self.username:
            print('MQTT | no username set in config.json; connecting anonymously', flush=True)
        self.start_worker(self.topic, 'MQTT', f'{self.host}:{self.port}')

    def write(self, snapshot):
        fields = snapshot_fields(snapshot)
        if not fields:
            return False
        payload = json.dumps({'timestamp': snapshot['timestamp'], 'measurement': 'deye',
                              'fields': fields}, allow_nan=False)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client.connect_timeout = 3
        if self.username:
            client.username_pw_set(self.username, self.password)
        pending = None

        def on_connect(client, userdata, flags, reason, properties):
            nonlocal pending
            if reason.is_failure:
                raise ValueError(f'Broker rejected connection: {reason}')
            pending = client.publish(self.topic, payload, qos=1)
            if pending.rc != mqtt.MQTT_ERR_SUCCESS:
                raise OSError(mqtt.error_string(pending.rc))

        def on_publish(client, userdata, mid, reason, properties):
            if reason.is_failure:
                raise ValueError(f'Broker rejected publication: {reason}')

        client.on_connect = on_connect
        client.on_publish = on_publish
        try:
            client.connect(self.host, self.port)
            deadline = monotonic() + 3
            while pending is None or not pending.is_published():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError('Broker did not acknowledge the message')
                result = client.loop(timeout=remaining)
                if result != mqtt.MQTT_ERR_SUCCESS:
                    raise OSError(mqtt.error_string(result))
        finally:
            client.disconnect()
        return True
