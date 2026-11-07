#!/usr/bin/env python3
"""Run the Core/Mobile development gate with the current Python environment.

Requires Core [dev,devices] and Mobile server/requirements.txt. No real API keys
or Android device are needed. This is protocol/loopback validation, not APK QA.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be finite and positive') from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('timeout must be finite and positive')
    return seconds


def _text(value) -> str:
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mobile', type=Path, required=True, help='whale-mobile checkout')
    parser.add_argument('--log-dir', type=Path, help='optional stage log directory')
    parser.add_argument('--contracts-only', action='store_true', help='skip loopback regression tests')
    parser.add_argument('--stage-timeout', type=positive_seconds, default=180.0, help='seconds allowed per stage (default 180)')
    args = parser.parse_args(argv)
    mobile = args.mobile.resolve()
    required = ('scripts/generate_capabilities.py', 'tests/contract_validate.py')
    if any(not (mobile / name).is_file() for name in required):
        parser.error('Mobile checkout is missing the interoperability tooling')
    run_dir = None
    if args.log_dir:
        args.log_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=args.log_dir.resolve()))
    python = sys.executable
    stages = [
        ('01-contract-mirror', ROOT, [python, 'scripts/contract_sync_check.py', '--core', str(ROOT), '--mobile', str(mobile)]),
        ('02-capability-tables', mobile, [python, 'scripts/generate_capabilities.py', '--core', str(ROOT), '--check']),
        ('03-schema-examples', mobile, [python, 'tests/contract_validate.py']),
    ]
    if not args.contracts_only:
        core_tests = [
            'tests/test_device_action_receipts.py', 'tests/test_device_agent_loop.py',
            'tests/test_device_schema.py',
            'tests/test_device_dispatch.py',
            'tests/test_device_request_ownership.py',
            'tests/unit/operations/test_device_interop_runner.py',
            'tests/test_device_receipt_results.py', 'tests/test_device_output_query.py',
            'tests/test_devices_registry.py', 'tests/test_devices_scopes.py',
            'tests/test_devices_ws.py', 'tests/test_durable_devices.py',
            'tests/test_arch_channels.py', 'tests/test_situation_autonomy.py',
        ]
        stages += [
            ('04-core-device-tests', ROOT, [python, '-m', 'pytest', *core_tests, '-q']),
            ('05-mobile-peer-tests', mobile, [python, '-m', 'pytest', 'tests/test_protocol_tooling.py', 'server/test_dev_gateway.py', '-q']),
        ]
    env = dict(os.environ, PYTHONUTF8='1')
    summary = {'ok': False, 'planned_stages': len(stages), 'stages': []}
    for name, cwd, command in stages:
        # A unique directory per invocation keeps pytest away from old results.
        if run_dir and '-m' in command and 'pytest' in command:
            command += ['--basetemp', str(run_dir / (name + '-temp'))]
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=args.stage_timeout)
            code, output = result.returncode, _text(result.stdout) + _text(result.stderr)
            status = 'passed' if code == 0 else 'failed'
        except subprocess.TimeoutExpired as exc:
            code, status = 124, 'timed_out'
            output = _text(exc.stdout) + _text(exc.stderr) + f'\nStage exceeded {args.stage_timeout}s.\n'
        except OSError as exc:
            code, status, output = 127, 'launch_error', f'{type(exc).__name__}: {exc}\n'
        except KeyboardInterrupt:
            code, status, output = 130, 'interrupted', 'Interrupted by user.\n'
        summary['stages'].append({'name': name, 'status': status, 'returncode': code,
                                  'seconds': round(time.monotonic() - started, 3)})
        summary['ok'] = code == 0 and len(summary['stages']) == len(stages)
        if run_dir:
            (run_dir / (name + '.log')).write_text(output, encoding='utf-8')
            (run_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
        print(f'{"PASS" if code == 0 else "FAIL"} {name} ({status})', flush=True)
        if code:
            print(output, end='')
            if run_dir:
                print(f'Results: {run_dir}')
            return code
    print(f'OK: {len(stages)} interoperability stages passed; no real device/model calls.')
    if run_dir:
        print(f'Results: {run_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
