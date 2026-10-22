import copy
from contextlib import redirect_stdout
import io
import sys
from unittest.mock import patch
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urlsplit, parse_qs

import inventory as core
from inventory_store import History
import netbox_inventory as manager

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT / 'examples/config.json').read_text())


def snap(version='1', observed='2026-10-21T10:00:00+00:00', node='42', label='web.example.org'):
    node_data = {'node_id': node, 'address': '192.0.2.42', 'location': 'Default', 'label': label, 'foreign_source': 'servers', 'foreign_id': 'server' + node}
    software = core.parse_walk(f'[{core.OID}].[1] = app_{version}_amd64\n')
    result = core.snapshot(config(), node_data, software, observed)
    result.update(observed_at=observed, synthetic=False)
    return result


class FakeNetBox(core.NetBox):
    def __init__(self):
        self.config = config()['netbox']
        self.objects = {'devices': [{'id': 123, 'name': 'web.example.org', 'site': {'id': 1}, 'tenant': {'id': 2}, 'custom_fields': {}}], 'virtual-machines': []}
        self.ips = [{'id': 1, 'address': '192.0.2.42/24', 'vrf': None, 'assigned_object_type': 'dcim.interface', 'assigned_object': {'device': {'id': 123}}}]
        self.fields, self.calls = [], []
        self.lose_next_patch = False

    def api(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        url = urlsplit(path)
        params = parse_qs(url.query)
        if url.path == '/api/extras/custom-fields/':
            if method == 'POST':
                record = dict(body, id=len(self.fields) + 1)
                self.fields.append(record)
                return copy.deepcopy(record)
            rows = [x for x in self.fields if x['name'] == params.get('name', [x['name']])[0]]
        elif url.path == '/api/ipam/ip-addresses/':
            rows = self.ips
        else:
            kind = 'devices' if '/dcim/devices/' in url.path else 'virtual-machines'
            parts = url.path.strip('/').split('/')
            if len(parts) == 4:
                obj = next(x for x in self.objects[kind] if x['id'] == int(parts[-1]))
                if method == 'PATCH':
                    obj['custom_fields'].update(copy.deepcopy(body['custom_fields']))
                    if self.lose_next_patch:
                        self.lose_next_patch = False
                        raise core.InventoryError('Simulated lost PATCH response')
                return copy.deepcopy(obj)
            rows = self.objects[kind]
            key = 'cf_' + self.config['identity_field']
            if key in params:
                rows = [x for x in rows if x['custom_fields'].get(self.config['identity_field']) == params[key][0]]
        offset, limit = int(params.get('offset', ['0'])[0]), int(params.get('limit', ['100'])[0])
        page = rows[offset:offset + limit]
        return {'count': len(rows), 'results': copy.deepcopy(page), 'next': '/next' if offset + len(page) < len(rows) else None}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = History(Path(self.temp.name) / 'history.sqlite3')
        self.addCleanup(self.db.db.close)

    def test_reimport_deduplicates_and_retains_each_version_transition(self):
        first = snap()
        self.assertTrue(self.db.observation(first))
        self.assertFalse(self.db.observation(first))
        self.db.observation(snap(observed='2026-10-21T11:00:00+00:00'))
        self.db.observation(snap('2', '2026-10-21T12:00:00+00:00'))
        self.db.observation(snap('1', '2026-10-21T13:00:00+00:00'))
        timeline = self.db.timeline('opennms-lab', '42')
        self.assertEqual(len(timeline), 3)
        self.assertEqual(timeline[0]['observations'], 2)
        stored = self.db.db.execute('SELECT snapshot FROM observations LIMIT 1').fetchone()[0]
        self.assertNotIn('software', json.loads(stored))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM contents').fetchone()[0], 2)
        self.assertEqual(timeline[1]['changes'][0]['kind'], 'version_changed')
        self.assertEqual(timeline[1]['changes'][0]['before'], ['1'])
        self.assertEqual(timeline[1]['changes'][0]['after'], ['2'])
        self.assertEqual(timeline[2]['changes'][0]['after'], ['1'])

    def test_old_import_does_not_replace_latest_observation(self):
        self.db.observation(snap('2', '2026-10-21T12:00:00+00:00'))
        self.db.observation(snap('1', '2026-10-21T10:00:00+00:00'))
        self.assertEqual(self.db.latest('opennms-lab')[0]['snapshot']['software'][0]['version'], '2')

    def test_invalid_hash_and_conflicting_timestamp_rejected(self):
        self.db.observation(snap())
        with self.assertRaises(ValueError):
            self.db.observation(snap('2'))
        bad = snap()
        bad['content_sha256'] = 'wrong'
        with self.assertRaises(ValueError):
            self.db.observation(bad)

    def test_failed_attempt_keeps_previous_success(self):
        self.db.observation(snap())
        self.db.attempt('opennms-lab', {'node_id': '42', 'attempted_at': '2026-10-21T12:00:00+00:00', 'collection': 'failed', 'reason': 'snmp_timeout'})
        host = self.db.latest('opennms-lab')[0]
        self.assertEqual(host['snapshot']['software'][0]['version'], '1')
        self.assertEqual(host['status']['reason'], 'snmp_timeout')

    def test_removal_is_no_longer_observed_not_uninstalled(self):
        self.db.observation(snap())
        other = snap('2', '2026-10-21T12:00:00+00:00')
        other['software'] = core.parse_walk(f'[{core.OID}].[1] = other_1_amd64\n')
        import hashlib
        other['content_sha256'] = hashlib.sha256(json.dumps(other['software'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.db.observation(other)
        kinds = {x['kind'] for x in self.db.timeline('opennms-lab', '42')[-1]['changes']}
        self.assertEqual(kinds, {'newly_observed', 'no_longer_observed'})

    def test_backup_is_consistent_and_refuses_overwrite(self):
        self.db.observation(snap())
        backup = Path(self.temp.name) / 'backup.sqlite3'
        self.db.backup(backup)
        with History(backup) as restored:
            self.assertEqual(len(restored.timeline('opennms-lab', '42')), 1)
        with self.assertRaises(ValueError):
            self.db.backup(backup)


class NetBoxWorkflowTests(unittest.TestCase):
    def setUp(self):
        StoreTests.setUp(self)
        self.config, self.client = config(), FakeNetBox()

    def prepared(self):
        self.db.observation(snap())
        manager.ensure_fields(self.client, self.config, apply=True)
        plan = manager.propose(self.db, self.client, self.config)
        plan['nodes'][0]['approve'] = True
        manager.confirm(self.db, self.client, self.config, plan, apply=True)
        self.client.calls.clear()
        return self.db.latest('opennms-lab')[0]

    def test_propose_is_read_only_and_confirm_is_local_only(self):
        self.db.observation(snap())
        plan = manager.propose(self.db, self.client, self.config)
        row = plan['nodes'][0]
        self.assertFalse(row['approve'])
        self.assertTrue(row['candidates'][0]['matched_name'])
        self.assertTrue(row['candidates'][0]['matched_ip'])
        with self.assertRaises(core.InventoryError):
            manager.confirm(self.db, self.client, self.config, plan, apply=True)
        row['approve'] = True
        manager.confirm(self.db, self.client, self.config, plan, apply=True)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))
        self.assertEqual(self.db.binding('opennms-lab', '42', manager.endpoint(self.config))['object_id'], 123)

    def test_scoped_matching_excludes_other_site_and_vrf(self):
        self.db.observation(snap())
        self.client.objects['devices'].append({'id': 124, 'name': 'web.example.org', 'site': {'id': 99}, 'tenant': {'id': 2}, 'custom_fields': {}})
        self.client.ips.append({'id': 2, 'address': '192.0.2.42/24', 'vrf': {'id': 9}, 'assigned_object_type': 'dcim.interface', 'assigned_object': {'device': {'id': 124}}})
        self.assertEqual(len(manager.propose(self.db, self.client, self.config)['nodes'][0]['candidates']), 2)
        self.config['netbox']['location_scope'] = {'Default': {'site_id': 1, 'vrf_id': None}}
        self.assertEqual([x['id'] for x in manager.propose(self.db, self.client, self.config)['nodes'][0]['candidates']], [123])

    def test_changed_evidence_and_duplicate_target_cannot_be_confirmed(self):
        self.db.observation(snap())
        plan = manager.propose(self.db, self.client, self.config)
        plan['nodes'][0]['approve'] = True
        self.db.observation(snap(observed='2026-10-21T12:00:00+00:00', label='renamed.example.org'))
        with self.assertRaises(core.InventoryError):
            manager.confirm(self.db, self.client, self.config, plan, apply=True)
        self.assertIsNone(self.db.binding('opennms-lab', '42', manager.endpoint(self.config)))

    def test_duplicate_target_identifies_both_hosts_and_saves_nothing(self):
        self.db.observation(snap())
        self.db.observation(snap(node='43', label='alias.example.org'))
        # A third valid selection must also remain uncommitted on batch failure.
        third = snap(node='44', label='separate.example.org')
        third['source']['address'] = '192.0.2.44'
        self.db.observation(third)
        self.client.objects['devices'].append({'id': 124, 'name': 'separate.example.org', 'custom_fields': {}})
        plan = manager.propose(self.db, self.client, self.config)
        for row in plan['nodes']:
            row['approve'] = True
        with self.assertRaises(manager.MappingConflict) as caught:
            manager.confirm(self.db, self.client, self.config, plan, apply=True)
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict['code'], 'duplicate_target')
        self.assertEqual(conflict['node_ids'], ['42', '43'])
        for text in ('web.example.org', 'alias.example.org', '#42', '#43', 'devices #123'):
            self.assertIn(text, str(caught.exception))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0], 0)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))
        # Deselecting only the duplicate allows both independent mappings.
        plan['nodes'][1]['approve'] = False
        self.assertEqual(manager.confirm(self.db, self.client, self.config, plan, apply=True)['bindings'], 2)

    def test_vanished_candidate_is_distinct_from_duplicate_target(self):
        self.db.observation(snap())
        plan = manager.propose(self.db, self.client, self.config)
        plan['nodes'][0]['approve'] = True
        self.client.objects['devices'].clear()
        with self.assertRaises(manager.MappingConflict) as caught:
            manager.confirm(self.db, self.client, self.config, plan, apply=True)
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict['code'], 'candidate_missing')
        self.assertEqual(conflict['node_ids'], ['42'])
        self.assertIn('web.example.org', conflict['message'])
        self.assertIn('devices #123', conflict['message'])
        self.assertIsNone(self.db.binding('opennms-lab', '42', manager.endpoint(self.config)))

    def test_occupied_candidate_reports_owner_instead_of_generic_evidence_change(self):
        self.db.observation(snap())
        plan = manager.propose(self.db, self.client, self.config)
        plan['nodes'][0]['approve'] = True
        self.client.objects['devices'][0]['custom_fields'][self.config['netbox']['identity_field']] = 'other-source:99'
        with self.assertRaises(manager.MappingConflict) as caught:
            manager.confirm(self.db, self.client, self.config, plan, apply=True)
        self.assertEqual(caught.exception.conflicts[0]['code'], 'target_occupied')
        self.assertIn('other-source:99', str(caught.exception))
        self.assertIsNone(self.db.binding('opennms-lab', '42', manager.endpoint(self.config)))

    def test_changed_candidate_identifies_node_and_target(self):
        self.db.observation(snap())
        plan = manager.propose(self.db, self.client, self.config)
        plan['nodes'][0]['approve'] = True
        self.client.objects['devices'][0]['name'] = 'renamed.example.org'
        with self.assertRaises(manager.MappingConflict) as caught:
            manager.confirm(self.db, self.client, self.config, plan, apply=True)
        self.assertEqual(caught.exception.conflicts[0]['code'], 'candidate_changed')
        self.assertIn('#42', str(caught.exception))
        self.assertIn('devices #123', str(caught.exception))

    def test_dry_run_and_repeated_sync_do_not_write(self):
        host = self.prepared()
        preview = manager.sync_one(self.db, self.client, self.config, host)
        self.assertEqual(preview['action'], 'would_update')
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.client.calls))
        result = manager.sync_one(self.db, self.client, self.config, host, apply=True)
        self.assertEqual(result['action'], 'confirmed')
        self.client.calls.clear()
        self.assertEqual(manager.sync_one(self.db, self.client, self.config, host, apply=True)['action'], 'unchanged')
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.client.calls))

    def test_new_timestamp_only_sends_metadata_when_software_unchanged(self):
        host = self.prepared()
        manager.sync_one(self.db, self.client, self.config, host, apply=True)
        self.db.observation(snap(observed='2026-10-21T11:00:00+00:00'))
        self.client.calls.clear()
        result = manager.sync_one(self.db, self.client, self.config, self.db.latest('opennms-lab')[0], apply=True)
        self.assertEqual(result['fields'], ['software_inventory_observed_at'])
        patch = next(body for method, _, body in self.client.calls if method == 'PATCH')
        self.assertNotIn(self.config['netbox']['field'], patch['custom_fields'])

    def test_missing_source_node_marks_status_without_deleting_asset_or_software(self):
        host = self.prepared()
        manager.sync_one(self.db, self.client, self.config, host, apply=True)
        host['present'] = False
        self.client.calls.clear()
        result = manager.sync_one(self.db, self.client, self.config, host, apply=True)
        self.assertEqual(result['fields'], ['software_inventory_status'])
        self.assertEqual(self.client.objects['devices'][0]['custom_fields']['software_inventory_status'], 'source_missing')
        self.assertTrue(all(method in ('GET', 'PATCH') for method, _, _ in self.client.calls))

    def test_lost_write_response_is_reconciled_without_second_patch(self):
        host = self.prepared()
        self.client.lose_next_patch = True
        with self.assertRaises(core.InventoryError):
            manager.sync_one(self.db, self.client, self.config, host, apply=True)
        self.client.calls.clear()
        self.assertEqual(manager.sync_one(self.db, self.client, self.config, host, apply=True)['action'], 'unchanged')
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.client.calls))

    def test_other_source_and_newer_netbox_observation_not_overwritten(self):
        host = self.prepared()
        self.client.objects['devices'][0]['custom_fields'][self.config['netbox']['identity_field']] = 'other:42'
        with self.assertRaises(core.InventoryError):
            manager.sync_one(self.db, self.client, self.config, host, apply=True)
        self.client.objects['devices'][0]['custom_fields'] = {'software_inventory_observed_at': '2026-10-22T00:00:00Z'}
        with self.assertRaises(core.InventoryError):
            manager.sync_one(self.db, self.client, self.config, host, apply=True)
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.client.calls))

    def test_synthetic_observation_never_exported(self):
        host = self.prepared()
        host['snapshot']['synthetic'] = True
        with self.assertRaises(core.InventoryError):
            manager.sync_one(self.db, self.client, self.config, host, apply=True)

    def test_setup_preserves_existing_fields_and_rejects_conflicts(self):
        manager.ensure_fields(self.client, self.config, apply=True)
        self.client.calls.clear()
        manager.ensure_fields(self.client, self.config, apply=True)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))
        self.client.fields[0]['type'] = 'integer'
        with self.assertRaises(core.InventoryError):
            manager.ensure_fields(self.client, self.config, apply=True)


class ImportAndBindingSafetyTests(unittest.TestCase):
    def setUp(self):
        StoreTests.setUp(self)
        self.config = config()

    def test_import_keeps_last_good_snapshot_for_currently_unreachable_host(self):
        state = Path(self.temp.name) / 'state'
        state.mkdir()
        first = snap()
        core.save_json(state / 'web--node-42.json', first)
        # A leftover file for a node no longer in OpenNMS must not re-add that node.
        core.save_json(state / 'removed--node-43.json', snap(node='43'))
        report = {'source_id': 'opennms-lab', 'started_at': '2026-10-21T12:00:00Z', 'enumeration': 'complete', 'nodes': [{'node_id': '42', 'attempted_at': '2026-10-21T12:00:01Z', 'collection': 'failed', 'reason': 'snmp_timeout'}]}
        core.save_json(state / 'inventory.json', report)
        result = manager.import_existing(self.db, self.config, state)
        self.assertEqual(result['new_observations'], 1)
        self.assertEqual(manager.import_existing(self.db, self.config, state)['already_imported'], 1)
        host = self.db.latest('opennms-lab')[0]
        self.assertEqual(host['status']['reason'], 'snmp_timeout')
        self.assertEqual(host['snapshot']['source']['node_id'], '42')
        self.assertEqual(self.db.timeline('opennms-lab', '43'), [])

    def test_report_removal_retains_history_and_old_run_does_not_resurrect_host(self):
        self.db.observation(snap())
        old = {'source_id': 'opennms-lab', 'started_at': '2026-10-21T10:00:00Z', 'enumeration': 'complete', 'nodes': [{'node_id': '42', 'attempted_at': '2026-10-21T10:00:01Z', 'collection': 'complete'}]}
        self.db.report(old)
        self.db.report({'source_id': 'opennms-lab', 'started_at': '2026-10-21T12:00:00Z', 'enumeration': 'complete', 'nodes': []})
        self.db.report(old)
        self.assertEqual(self.db.latest('opennms-lab'), [])
        self.assertEqual(len(self.db.timeline('opennms-lab', '42')), 1)

    def test_binding_target_uniqueness_is_transactional(self):
        target = {'type': 'devices', 'id': 123}
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.bind_many('opennms-lab', manager.endpoint(self.config), [{'node_id': '42', 'target': target}, {'node_id': '43', 'target': target}])
        self.assertIsNone(self.db.binding('opennms-lab', '42', manager.endpoint(self.config)))

    def test_netbox_candidate_replacement_after_review_is_refused(self):
        client = FakeNetBox()
        self.db.observation(snap())
        plan = manager.propose(self.db, client, self.config)
        plan['nodes'][0]['approve'] = True
        # Same ID and same IP, but the candidate now has another name.
        client.objects['devices'][0]['name'] = 'replacement.example.org'
        with self.assertRaises(core.InventoryError):
            manager.confirm(self.db, client, self.config, plan, apply=True)

    def test_changed_foreign_identity_is_not_silently_exported(self):
        client = FakeNetBox()
        self.db.observation(snap())
        plan = manager.propose(self.db, client, self.config)
        plan['nodes'][0]['approve'] = True
        manager.confirm(self.db, client, self.config, plan, apply=True)
        later = snap(observed='2026-10-21T12:00:00Z')
        later['host']['foreign_id'] = 'different-machine'
        self.db.observation(later)
        client.calls.clear()
        with self.assertRaises(core.InventoryError):
            manager.sync_one(self.db, client, self.config, self.db.latest('opennms-lab')[0], apply=True)
        self.assertEqual(client.calls, [])


class CliWorkflowTests(unittest.TestCase):
    def test_scheduled_sync_skips_unbound_hosts_without_hiding_bound_host_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = root / 'config.json'
            core.save_json(cfg, config())
            client = FakeNetBox()
            manager.ensure_fields(client, config(), apply=True)
            with History(root / 'state/opennms-lab/history.sqlite3') as db:
                db.observation(snap())
                db.observation(snap(node='43', label='new.example.org'))
                plan = manager.propose(db, client, config())
                plan['nodes'][0]['approve'] = True
                manager.confirm(db, client, config(), plan, apply=True)
            argv = ['netbox_inventory.py', '--config', str(cfg), '--state', str(root / 'state'), 'sync', '--confirmed-only', '--apply']
            with patch.object(sys, 'argv', argv), patch.object(core, 'NetBox', return_value=client), redirect_stdout(io.StringIO()):
                self.assertEqual(manager.main(), 0)
                rows = core.load_json(root / 'state/opennms-lab/netbox-sync.json')['results']
                self.assertEqual([row['action'] for row in rows], ['confirmed', 'skipped'])
                self.assertEqual(rows[1]['reason'], 'no_confirmed_binding')
                client.objects['devices'][0]['custom_fields'][config()['netbox']['identity_field']] = 'other:42'
                self.assertEqual(manager.main(), 1)
            with History(root / 'state/opennms-lab/history.sqlite3') as db:
                self.assertIsNone(db.binding('opennms-lab', '43', manager.endpoint(config())))

    def test_existing_files_to_reviewed_binding_and_repeated_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state/opennms-lab'
            state.mkdir(parents=True)
            cfg = root / 'config.json'
            core.save_json(cfg, config())
            core.save_json(state / 'web--node-42.json', snap())
            core.save_json(state / 'inventory.json', {'source_id': 'opennms-lab', 'started_at': '2026-10-21T10:00:00Z', 'enumeration': 'complete', 'nodes': [{'node_id': '42', 'attempted_at': '2026-10-21T10:00:00Z', 'observed_at': '2026-10-21T10:00:00Z', 'collection': 'complete', 'snapshot_file': 'web--node-42.json'}]})
            client = FakeNetBox()
            def command(*args):
                argv = ['netbox_inventory.py', '--config', str(cfg), '--state', str(root / 'state'), *args]
                with patch.object(sys, 'argv', argv), patch.object(core, 'NetBox', return_value=client), redirect_stdout(io.StringIO()):
                    self.assertEqual(manager.main(), 0)
            command('import')
            command('setup-fields', '--apply')
            plan = root / 'mapping.json'
            command('propose', '--output', str(plan))
            data = core.load_json(plan)
            data['nodes'][0]['approve'] = True
            core.save_json(plan, data)
            command('confirm', '--plan', str(plan), '--apply')
            client.calls.clear()
            command('sync')
            self.assertFalse(any(method == 'PATCH' for method, _, _ in client.calls))
            command('sync', '--apply')
            client.calls.clear()
            command('sync', '--apply')
            self.assertFalse(any(method == 'PATCH' for method, _, _ in client.calls))
            command('history', '--node', '42')
            command('backup', '--output', str(root / 'backup.sqlite3'))


if __name__ == '__main__':
    unittest.main()
