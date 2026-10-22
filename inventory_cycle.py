#!/usr/bin/env python3
"""Run an unattended collection and sync only previously confirmed bindings."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys

import inventory as core

ROOT = Path(__file__).resolve().parent


def run_cycle(config_path, state_root, timeout=180, password_file=None, apply=False):
    config_path, state_root = Path(config_path).resolve(), Path(state_root).resolve()
    config = core.load_json(config_path)
    core.validate(config)
    if 'netbox' not in config:
        raise core.InventoryError('Add NetBox connection settings before scheduling a collection/export cycle')
    state = state_root / config['source_id']
    state.mkdir(parents=True, exist_ok=True)
    with (state / '.cycle.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise core.InventoryError('Another inventory cycle is running') from None
        summary = {'started_at': core.now(), 'source_id': config['source_id'], 'apply': apply, 'status': 'running', 'stage': 'preflight'}
        status_path = state / 'cycle.status.json'
        core.save_json(status_path, summary)
        try:
            # Validate local credentials before starting a potentially long scan.
            core.OpenNMS(config['opennms'])
            core.NetBox(config['netbox'])
            if password_file:
                core.read_ssh_password(password_file)
            report_path = state / 'inventory.json'
            previous = core.load_json(report_path).get('started_at') if report_path.exists() else None
            common = ['--config', str(config_path), '--state', str(state_root)]
            command = [sys.executable, '-u', str(ROOT / 'inventory.py'), *common, '--timeout', str(timeout)]
            if password_file:
                command += ['--ssh-password-file', str(Path(password_file).absolute())]
            summary['stage'] = 'collection'
            core.save_json(status_path, summary)
            collected = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
            summary['collector_exit'] = collected.returncode
            if collected.returncode not in (0, 1):
                raise core.InventoryError('Collector failed; no NetBox synchronization attempted. See service log.')
            # Exit 1 may mean individual unreachable hosts. Require a new complete
            # discovery report so a crash cannot accidentally reuse an older run.
            report = core.load_json(report_path)
            if (report.get('source_id') != config['source_id'] or report.get('enumeration') != 'complete'
                    or report.get('stopped') or not report.get('finished_at') or report.get('synthetic')
                    or not report.get('started_at') or report['started_at'] == previous):
                raise core.InventoryError('No new completed collector report; NetBox synchronization was not started')
            summary['collection'] = {key: report.get(key) for key in ('discovered', 'collected', 'skipped', 'errors')}
            summary['stage'] = 'sync'
            core.save_json(status_path, summary)
            command = [sys.executable, '-u', str(ROOT / 'netbox_inventory.py'), *common, 'sync', '--confirmed-only']
            if apply:
                command.append('--apply')
            synced = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
            summary['sync_exit'] = synced.returncode
            if synced.returncode:
                raise core.InventoryError('NetBox synchronization reported errors. See service log and netbox-sync.json.')
            summary.update(status='partial' if collected.returncode else 'complete', stage='finished')
            return 1 if collected.returncode else 0
        except (core.InventoryError, OSError, ValueError, KeyError, TypeError) as exc:
            summary.update(status='failed', error=str(exc) if isinstance(exc, core.InventoryError) else type(exc).__name__)
            return 2
        finally:
            summary['finished_at'] = core.now()
            core.save_json(status_path, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', default='state')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--ssh-password-file')
    parser.add_argument('--apply', action='store_true', help='Write confirmed inventory fields to NetBox; default is a preview')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 3600:
        parser.error('--timeout must be between 1 and 3600 seconds')
    return run_cycle(args.config, args.state, args.timeout, args.ssh_password_file, args.apply)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (core.InventoryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc) if isinstance(exc, core.InventoryError) else 'Cannot start cycle: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(2)
