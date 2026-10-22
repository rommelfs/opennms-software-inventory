import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import inventory as core
import inventory_cycle as cycle

ROOT = Path(__file__).resolve().parents[1]


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.config = core.load_json(ROOT / 'examples/config.json')
        core.save_json(self.config_path, self.config)
        self.state = self.root / 'state' / self.config['source_id']
        self.state.mkdir(parents=True)
        self.report = {'source_id': self.config['source_id'], 'started_at': '2026-10-22T02:00:00Z',
                       'finished_at': '2026-10-22T02:05:00Z', 'enumeration': 'complete',
                       'synthetic': False, 'discovered': 2, 'collected': 1, 'skipped': 0, 'errors': 1}

    def run_cycle(self, collector_exit=0, report=True, sync_exit=0, apply=False):
        def run(argv, **kwargs):
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            if argv[2].endswith('/inventory.py'):
                if report:
                    core.save_json(self.state / 'inventory.json', self.report)
                return subprocess.CompletedProcess(argv, collector_exit)
            self.assertIn('--confirmed-only', argv)
            self.assertEqual('--apply' in argv, apply)
            return subprocess.CompletedProcess(argv, sync_exit)
        with patch.object(core, 'OpenNMS'), patch.object(core, 'NetBox'), patch.object(cycle.subprocess, 'run', side_effect=run) as called, redirect_stdout(io.StringIO()):
            result = cycle.run_cycle(self.config_path, self.root / 'state', apply=apply)
        return result, called

    def test_success_syncs_confirmed_only_and_defaults_to_preview(self):
        result, called = self.run_cycle()
        self.assertEqual(result, 0)
        self.assertEqual(called.call_count, 2)
        self.assertEqual(core.load_json(self.state / 'cycle.status.json')['status'], 'complete')

    def test_partial_scan_still_syncs_and_reports_partial_failure(self):
        result, called = self.run_cycle(collector_exit=1, apply=True)
        self.assertEqual(result, 1)
        self.assertEqual(called.call_count, 2)
        self.assertEqual(core.load_json(self.state / 'cycle.status.json')['status'], 'partial')

    def test_fatal_collector_failure_never_syncs(self):
        result, called = self.run_cycle(collector_exit=2)
        self.assertEqual(result, 2)
        self.assertEqual(called.call_count, 1)

    def test_crash_with_old_report_never_syncs(self):
        core.save_json(self.state / 'inventory.json', self.report)
        result, called = self.run_cycle(collector_exit=1, report=False)
        self.assertEqual(result, 2)
        self.assertEqual(called.call_count, 1)

    def test_incomplete_foreign_synthetic_and_aborted_reports_never_sync(self):
        original = copy.deepcopy(self.report)
        for changes in ({'enumeration': 'failed'}, {'source_id': 'other'}, {'synthetic': True}, {'finished_at': None}, {'stopped': 'ssh_authentication_failed'}):
            with self.subTest(changes=changes):
                self.report = dict(original, **changes)
                (self.state / 'inventory.json').unlink(missing_ok=True)
                result, called = self.run_cycle(collector_exit=1)
                self.assertEqual(result, 2)
                self.assertEqual(called.call_count, 1)

    def test_sync_failure_is_visible(self):
        result, called = self.run_cycle(sync_exit=1, apply=True)
        self.assertEqual(result, 2)
        summary = core.load_json(self.state / 'cycle.status.json')
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['stage'], 'sync')

    def test_overlapping_cycle_is_rejected_without_overwriting_status(self):
        import fcntl
        status = self.state / 'cycle.status.json'
        core.save_json(status, {'status': 'running', 'marker': 'first cycle'})
        with (self.state / '.cycle.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.InventoryError, 'Another inventory cycle'):
                cycle.run_cycle(self.config_path, self.root / 'state')
        self.assertEqual(core.load_json(status)['marker'], 'first cycle')


class PasswordFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'password'
        self.path.write_text('synthetic-password with spaces\n')
        self.path.chmod(0o600)

    def test_reads_private_file_without_stripping_password_spaces(self):
        self.path.write_text('  synthetic password  \n')
        self.assertEqual(core.read_ssh_password(self.path), '  synthetic password  ')

    def test_rejects_readable_by_others_and_symlinks(self):
        self.path.chmod(0o640)
        with self.assertRaises(core.InventoryError):
            core.read_ssh_password(self.path)
        self.path.chmod(0o600)
        link = self.path.parent / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(core.InventoryError):
            core.read_ssh_password(link)

    def test_rejects_wrong_owner_and_nonregular_file(self):
        with patch.object(core.os, 'geteuid', return_value=self.path.stat().st_uid + 1), self.assertRaises(core.InventoryError):
            core.read_ssh_password(self.path)
        fifo = self.path.parent / 'pipe'
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(core.InventoryError):
            core.read_ssh_password(fifo)

    def test_rejects_invalid_files_without_disclosing_contents(self):
        for text in ('', 'synthetic\nextra', 'synthetic\x00password', 'x' * 4098):
            self.path.write_text(text)
            with self.subTest(length=len(text)), self.assertRaises(core.InventoryError) as caught:
                core.read_ssh_password(self.path)
            self.assertNotIn('synthetic', str(caught.exception))

    def test_password_file_uses_askpass_without_prompting_or_disclosing_secret(self):
        config = {'ssh_target': 'opennms-karaf'}
        with patch.object(core.getpass, 'getpass') as prompt:
            try:
                with core.ssh_session(config, password_file=self.path):
                    env = config['_ssh_password_env']
                    self.assertNotIn('synthetic-password with spaces', env.values())
                    helper = Path(env['SSH_ASKPASS'])
                    self.assertNotIn('synthetic-password', helper.read_text())
                    result = subprocess.run([str(helper), 'Password:'], env=env, capture_output=True, check=True)
                    self.assertEqual(result.stdout, b'synthetic-password with spaces\n')
            except PermissionError:
                self.skipTest('Sandbox blocks local Unix sockets')
        prompt.assert_not_called()
        self.assertNotIn('_ssh_password_env', config)


if __name__ == '__main__':
    unittest.main()
