from contextlib import contextmanager
import copy
from http.client import parse_headers
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from types import SimpleNamespace
import unittest

import inventory as core
from inventory_store import History
import inventory_web as web
import netbox_inventory as manager
from test_store_and_netbox import FakeNetBox, config, snap


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config, self.client = config(), FakeNetBox()
        self.config['netbox']['location_scope'] = {'Default': {'site_id': 1}}
        self.state = Path(self.temp.name)
        with History(self.state / 'history.sqlite3') as db:
            db.observation(snap())
        self.app = web.Application(self.config, self.state, client_factory=lambda: self.client)

    def test_view_contains_hostname_candidates_and_no_software_payload_or_secret(self):
        self.app.refresh()
        data = self.app.state_data()
        row = data['nodes'][0]
        self.assertEqual(row['label'], 'web.example.org')
        self.assertEqual(row['package_count'], 1)
        self.assertTrue(row['candidates'][0]['matched_name'])
        self.assertTrue(row['candidates'][0]['matched_ip'])
        self.assertEqual(data['bulk_eligible'], ['42'])
        text = json.dumps(data)
        self.assertNotIn('Authorization', text)
        self.assertNotIn(self.app.token, text)
        self.assertNotIn('"software":', text)

    def test_dns_name_is_an_independent_candidate_signal(self):
        self.client.objects['devices'][0]['name'] = 'different-asset-label'
        self.client.ips[0]['address'] = '192.0.2.99/24'
        self.client.ips[0]['dns_name'] = 'WEB.EXAMPLE.ORG.'
        self.app.refresh()
        row = self.app.state_data()['nodes'][0]
        self.assertEqual(len(row['candidates']), 1)
        self.assertTrue(row['candidates'][0]['matched_dns'])
        self.assertFalse(row['candidates'][0]['matched_name'])
        self.assertFalse(row['candidates'][0]['matched_ip'])
        self.assertEqual(self.app.state_data()['bulk_eligible'], [])

    def test_binding_from_click_is_local_and_rejects_unlisted_target(self):
        self.app.refresh()
        with self.assertRaises(core.InventoryError):
            self.app.save_bindings([{'node_id': '42', 'target': {'type': 'devices', 'id': 999}}])
        self.app.save_bindings([{'node_id': '42', 'target': {'type': 'devices', 'id': 123}}])
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))
        data = self.app.state_data()
        self.assertEqual(data['nodes'][0]['existing_binding']['object_id'], 123)
        self.assertEqual(data['bulk_eligible'], [])

    def test_no_bulk_selection_for_private_address_without_scope_or_occupied_target(self):
        self.config['netbox'].pop('location_scope')
        self.app.refresh()
        self.assertEqual(self.app.state_data()['bulk_eligible'], [])
        self.config['netbox']['location_scope'] = {'Default': {'site_id': 1}}
        self.client.objects['devices'][0]['custom_fields'][self.config['netbox']['identity_field']] = 'other:42'
        self.app.refresh()
        self.assertEqual(self.app.state_data()['bulk_eligible'], [])
        with self.assertRaises(core.InventoryError):
            self.app.save_bindings([{'node_id': '42', 'target': {'type': 'devices', 'id': 123}}])

    def test_browser_changes_cannot_forge_candidate_evidence(self):
        self.app.refresh()
        self.client.objects['devices'][0]['name'] = 'replacement'
        with self.assertRaises(core.InventoryError):
            self.app.save_bindings([{'node_id': '42', 'target': {'type': 'devices', 'id': 123}}])

    def test_export_requires_preview_and_rejects_changed_observation(self):
        self.app.refresh()
        self.app.save_bindings([{'node_id': '42', 'target': {'type': 'devices', 'id': 123}}])
        manager.ensure_fields(self.client, self.config, apply=True)
        with self.assertRaises(core.InventoryError):
            self.app.sync(True)
        preview = self.app.sync(False)
        self.assertTrue(preview['preview_id'])
        self.assertEqual(preview['results'][0]['action'], 'would_update')
        with History(self.state / 'history.sqlite3') as db:
            db.observation(snap('2', '2026-10-22T12:00:00Z'))
        with self.assertRaises(core.InventoryError):
            self.app.sync(True, preview['preview_id'])
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.client.calls))

    def test_preview_apply_then_new_preview_is_idempotent(self):
        self.app.refresh()
        self.app.save_bindings([{'node_id': '42', 'target': {'type': 'devices', 'id': 123}}])
        manager.ensure_fields(self.client, self.config, apply=True)
        preview = self.app.sync(False)
        result = self.app.sync(True, preview['preview_id'])
        self.assertEqual(result['results'][0]['action'], 'confirmed')
        self.client.calls.clear()
        second = self.app.sync(False)
        self.assertEqual(second['results'][0]['action'], 'unchanged')
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.client.calls))

    def test_node_detail_reads_only_requested_history(self):
        with History(self.state / 'history.sqlite3') as db:
            db.observation(snap('2', '2026-10-22T12:00:00Z'))
        detail = self.app.node_detail('42')
        self.assertEqual(detail['snapshot']['software'][0]['version'], '2')
        self.assertEqual(len(detail['timeline']), 2)

    def request(self, method, path, *, authorized=True, host='127.0.0.1:8765', origin=None, body=None):
        handler = object.__new__(web.make_handler(self.app))
        handler.server = SimpleNamespace(server_port=8765)
        handler.request_version, handler.requestline = 'HTTP/1.1', method + ' ' + path
        handler.command, handler.path = method, path
        payload = json.dumps(body).encode() if body is not None else b''
        headers = {'Host': host, 'Content-Type': 'application/json', 'Content-Length': str(len(payload))}
        if authorized:
            headers['Authorization'] = 'Bearer ' + self.app.token
        if origin:
            headers['Origin'] = origin
        handler.headers = parse_headers(io.BytesIO((''.join(k + ': ' + v + '\r\n' for k, v in headers.items()) + '\r\n').encode()))
        handler.rfile, handler.wfile = io.BytesIO(payload), io.BytesIO()
        getattr(handler, 'do_' + method)()
        raw = handler.wfile.getvalue()
        header, data = raw.split(b'\r\n\r\n', 1)
        return int(header.split(b' ')[1]), header, data

    def test_http_boundary_rejects_missing_token_foreign_origin_and_host(self):
        for kwargs in ({'authorized': False}, {'origin': 'https://foreign.example'}, {'host': 'evil.example:8765'}):
            code, _, _ = self.request('GET', '/api/state', **kwargs)
            self.assertIn(code, (401, 403))
        code, headers, data = self.request('GET', '/api/state')
        self.assertEqual(code, 200)
        self.assertIn(b'no-store', headers)
        self.assertEqual(json.loads(data)['source_id'], 'opennms-lab')

    def test_unauthorized_write_cannot_start_jobs(self):
        code, _, _ = self.request('POST', '/api/bind', authorized=False, body={'selections': []})
        self.assertEqual(code, 403)
        self.assertEqual(self.app.jobs, {})

    def wait_job(self, identity):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.app.job_lock:
                job = copy.deepcopy(self.app.jobs[identity])
            if job['status'] != 'running':
                return job
            time.sleep(.01)
        self.fail('Background job did not complete')

    def test_authorized_http_refresh_runs_background_job_without_netbox_writes(self):
        code, _, data = self.request('POST', '/api/refresh', body={})
        self.assertEqual(code, 202)
        job = self.wait_job(json.loads(data)['job_id'])
        self.assertEqual(job['status'], 'done')
        self.assertIsNotNone(self.app.plan)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))

    def test_second_mutation_cannot_run_while_first_is_active(self):
        release = threading.Event()
        with patch.object(self.app, 'refresh', side_effect=lambda: release.wait(5)):
            started = self.app.start_job('refresh', {})
            try:
                with self.assertRaises(core.InventoryError):
                    self.app.start_job('import', {})
            finally:
                release.set()
                self.wait_job(started['job_id'])

    def test_static_files_are_explicitly_allowlisted(self):
        code, headers, html = self.request('GET', '/', authorized=False)
        self.assertEqual(code, 200)
        self.assertIn(b'Content-Security-Policy', headers)
        self.assertNotIn(self.app.token.encode(), html)
        code, _, _ = self.request('GET', '/../config.json')
        self.assertEqual(code, 404)


class DemoTests(unittest.TestCase):
    def test_demo_uses_isolated_history_and_never_exports(self):
        from inventory_web_demo import create_demo
        with tempfile.TemporaryDirectory() as temp:
            app = create_demo(Path(temp))
            self.assertEqual(len(app.state_data()['nodes']), 8)
            self.assertTrue(app.state_data()['demo'])
            self.assertTrue(any(c.get('matched_dns') for row in app.state_data()['nodes'] for c in row['candidates']))
            with self.assertRaises(core.InventoryError):
                app.sync(True)


if __name__ == '__main__':
    unittest.main()
