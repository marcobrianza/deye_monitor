#!/usr/bin/env python3
"""Read one Deye FC03 snapshot using the Modbus TCP transport."""
import argparse
from datetime import datetime
import fcntl
import json
from pathlib import Path
import sys
import time

from modbus_transport import TCPInstrument, mm

BASE = Path(__file__).resolve().parent


def scan(instrument, ranges, block_size, pause):
    registers = {}
    rejected = []
    transport_error = None

    def read_block(start, count):
        time.sleep(pause)
        try:
            values = instrument.read_registers(start, count, functioncode=3)
        except mm.SlaveReportedException as exc:
            if str(exc) not in ('Modbus exception 0x02', 'Modbus exception 0x03'):
                raise
            if count == 1:
                rejected.append([start, str(exc)])
                return
            left = count // 2
            read_block(start, left)
            read_block(start + left, count - left)
        else:
            registers.update(zip(range(start, start + count), values))
            return

    try:
        for first, last in ranges:
            for start in range(first, last + 1, block_size):
                read_block(start, min(block_size, last - start + 1))
    except (OSError, mm.SlaveReportedException) as exc:
        transport_error = str(exc)
    return registers, rejected, transport_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=BASE / 'config.json')
    parser.add_argument('--output', type=Path, default=BASE / 'deye_dump.jsonl')
    parser.add_argument('--port', type=int, help='Override the configured TCP port')
    parser.add_argument('--range', nargs=2, type=int, action='append',
                        metavar=('FIRST', 'LAST'), help='Override register ranges')
    parser.add_argument('--probe', action='store_true', help='Read only register 0')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
    except FileNotFoundError:
        parser.error(f'Configuration file not found: {args.config}. '
                     'Copy config.default.json to config.json for the default setup.')
    port = config['port'] if args.port is None else args.port
    unit = config['unit_id']
    size = config['block_size']
    ranges = [[0, 0]] if args.probe else (args.range or config['ranges'])
    if not (1 <= port <= 65535 and 1 <= unit <= 247 and 1 <= size <= 125):
        parser.error('Invalid TCP port, unit ID or block size')
    if not ranges or any(not 0 <= first <= last <= 65535 for first, last in ranges):
        parser.error('Invalid register ranges')
    if args.probe:
        return dump(args, config, port, unit, size, ranges)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.output.with_suffix(args.output.suffix + '.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A scan for this output file is already running', file=sys.stderr)
            return 3
        return dump(args, config, port, unit, size, ranges)


def dump(args, config, port, unit, size, ranges):
    timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
    started = time.monotonic()
    instrument = TCPInstrument(config['host'], port, unit, config['timeout_seconds'])
    registers, rejected, error = scan(
        instrument, ranges, size, config['request_pause_seconds']
    )
    snapshot = {
        'timestamp': timestamp,
        'model': config['model'],
        'host': config['host'],
        'port': port,
        'unit_id': unit,
        'function': 3,
        'requested_ranges': ranges,
        'duration_seconds': round(time.monotonic() - started, 2),
        'registers': registers,
        'rejected': rejected,
        'error': error,
    }
    if args.probe:
        print(json.dumps(snapshot, indent=2))
    else:
        with args.output.open('a', encoding='utf-8') as output:
            output.write(json.dumps(snapshot, separators=(',', ':')) + '\n')
        print(f'{timestamp} | {len(registers)} read, {len(rejected)} rejected | {args.output}')
    if error:
        print(error, file=sys.stderr)
    return 0 if registers and error is None else 2


if __name__ == '__main__':
    sys.exit(main())
