#!/usr/bin/env python3
"""Run the Core/Mobile development gate with the current Python environment.

Requires Core [dev,devices] and Mobile server/requirements.txt. No real API keys
or Android device are needed. This is protocol/loopback validation, not APK QA.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mobile', type=Path, required=True, help='whale-mobile checkout')
    parser.add_argument('--log-dir', type=Path, help='optional stage log directory')
    parser.add_argument('--contracts-only', action='store_true', help='skip loopback regression tests')
    args = parser.parse_args(argv)
    mobile = args.mobile.resolve()
    required = ('scripts/generate_capabilities.py', 'tests/contract_validate.py')
    if any(not (mobile / name).is_file() for name in required):
        parser.error('Mobile checkout is missing the interoperability tooling')
    if args.log_dir:
        args.log_dir.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    stages = [
        ('01-contract-mirror', ROOT, [python, 'scripts/contract_sync_check.py', '--core', str(ROOT), '--mobile', str(mobile)]),
        ('02-capability-tables', mobile, [python, 'scripts/generate_capabilities.py', '--core', str(ROOT), '--check']),
        ('03-schema-examples', mobile, [python, 'tests/contract_validate.py']),
    ]
    if not args.contracts_only:
        core_tests = [
            'tests/test_device_action_receipts.py', 'tests/test_device_agent_loop.py',
            'tests/test_device_request_ownership.py',
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
    for name, cwd, command in stages:
        # Keep pytest temporary artifacts beside the requested stage logs.
        if args.log_dir and '-m' in command and 'pytest' in command:
            command += ['--basetemp', str(args.log_dir.resolve() / (name + '-temp'))]
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if args.log_dir:
            (args.log_dir / (name + '.log')).write_text(output, encoding='utf-8')
        print(f'{"PASS" if result.returncode == 0 else "FAIL"} {name}', flush=True)
        if result.returncode:
            print(output, end='')
            return result.returncode
    print(f'OK: {len(stages)} interoperability stages passed; no real device/model calls.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
