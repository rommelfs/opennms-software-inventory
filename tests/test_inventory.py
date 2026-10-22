import copy
from contextlib import ExitStack
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import inventory as app

ROOT = Path(__file__).resolve().parents[1]


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "examples/config.json").read_text())
        self.node = {"node_id": "42", "address": "192.0.2.42", "location": "Branch Berlin", "netbox": {"type": "virtual-machines", "id": 123}}
        self.text = (ROOT / "examples/walk.txt").read_text()

    def test_versions_epoch_architecture_and_unknown(self):
        rows = app.parse_walk(self.text + f"[{app.OID}].[6] = libx_2:1.0~rc1-2_arm64\n")
        item = next(x for x in rows if x["name"] == "libx")
        self.assertEqual(item["version"], "2:1.0~rc1-2")
        self.assertEqual(item["architecture"], "arm64")
        self.assertIsNone(next(x for x in rows if x["name"].startswith("custom"))["version"])

    def test_no_partial_empty_or_ambiguous_success(self):
        for text in ["", "...", self.text + "host: timeout", "Unknown host", self.text + self.text]:
            with self.subTest(text=text[:30]), self.assertRaises(app.InventoryError):
                app.parse_walk(text)

    def test_duplicate_package_deduplicated(self):
        rows = app.parse_walk(f"[{app.OID}].[1] = x_1_amd64\n[{app.OID}].[2] = x_1_amd64\n")
        self.assertEqual(len(rows), 1)

    def test_location_and_command_injection_rejected(self):
        for location in ['Berlin"; shutdown', 'Berlin$(id)', "Berlin\nexit"]:
            self.node["location"] = location
            with self.assertRaises(app.InventoryError):
                app.validate_node(self.node)

    def test_manual_host_lists_rejected(self):
        self.config["nodes"] = [self.node]
        with self.assertRaises(app.InventoryError):
            app.validate(self.config)

    def test_ipv6_zone_cannot_inject_remote_command(self):
        self.node["address"] = "fe80::1%$(id)"
        with self.assertRaises(app.InventoryError):
            app.validate_node(self.node)

    def test_walk_uses_opennms_location_and_no_server_credentials(self):
        def fake_run(argv, **kwargs):
            self.assertEqual(argv[-1], f'opennms:snmp-walk -l "Branch Berlin" 192.0.2.42 .{app.OID}')
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            kwargs["stdout"].write(self.text.encode())
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(app.subprocess, "run", side_effect=fake_run):
            self.assertEqual(len(app.walk(self.config, self.node, 10)), 5)

    def client(self):
        client = object.__new__(app.NetBox)
        client.config = self.config["netbox"]
        return client

    def test_dry_run_does_not_patch(self):
        client = self.client()
        record = {"id": 123, "custom_fields": {"opennms_node_key": "opennms-lab:42", client.config["field"]: None}}
        snap = app.snapshot(self.config, self.node, app.parse_walk(self.text), app.now())
        with patch.object(client, "request", return_value=record) as request:
            self.assertEqual(client.sync(snap), "would update")
            self.assertEqual([x.args[0] for x in request.call_args_list], ["GET"])

    def test_confirmed_write_only_owns_one_field(self):
        client = self.client()
        field = client.config["field"]
        snap = app.snapshot(self.config, self.node, app.parse_walk(self.text), app.now())
        before = {"id": 123, "custom_fields": {"opennms_node_key": "opennms-lab:42", field: None, "owner": "team"}}
        after = {"id": 123, "custom_fields": {"opennms_node_key": "opennms-lab:42", field: snap, "owner": "team"}}
        with patch.object(client, "request", side_effect=[before, after, after]) as request:
            self.assertEqual(client.sync(snap, apply=True), "confirmed")
            self.assertEqual(request.call_args_list[1].args[2], {"custom_fields": {field: snap}})

    def test_foreign_source_not_overwritten(self):
        client = self.client()
        snap = app.snapshot(self.config, self.node, app.parse_walk(self.text), app.now())
        record = {"id": 123, "custom_fields": {"opennms_node_key": "opennms-lab:42", client.config["field"]: {"source": {"id": "other", "node_id": "42"}}}}
        with patch.object(client, "request", return_value=record) as request:
            with self.assertRaises(app.InventoryError):
                client.sync(snap, apply=True)
            self.assertEqual(request.call_count, 1)

    def test_lost_write_response_reconciles_by_read(self):
        client = self.client()
        snap = app.snapshot(self.config, self.node, app.parse_walk(self.text), app.now())
        record = {"id": 123, "custom_fields": {"opennms_node_key": "opennms-lab:42", client.config["field"]: snap}}
        with patch.object(client, "request", return_value=record) as request:
            self.assertEqual(client.sync(snap, apply=True), "unchanged")
            self.assertEqual(request.call_count, 1)

    def test_failure_keeps_last_snapshot_and_other_nodes_continue(self):
        second = copy.deepcopy(self.node)
        second["node_id"] = 43
        second["netbox"]["id"] = 124
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = root / "config.json"
            app.save_json(cfg, self.config)
            previous = root / "opennms-lab/node-42.json"
            app.save_json(previous, {"previous": "snapshot"})
            argv = ["inventory.py", "--config", str(cfg), "--state", temp]
            with patch.object(sys, "argv", argv), patch.object(app.OpenNMS, "__init__", return_value=None), patch.object(app.OpenNMS, "nodes", return_value=[{"id": "42"}, {"id": "43"}]), patch.object(app.OpenNMS, "resolve", side_effect=[self.node, second]), patch.object(app, "walk", side_effect=[app.SnmpTimeout("timeout"), app.parse_walk(self.text)]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(), 1)
            self.assertEqual(app.load_json(previous), {"previous": "snapshot"})
            self.assertEqual(app.load_json(root / "opennms-lab/node-43.json")["source"]["node_id"], "43")
            self.assertEqual(app.load_json(root / "opennms-lab/node-42.status.json")["error"], "timeout")
            self.assertEqual(app.load_json(root / "opennms-lab/node-42.status.json")["reason"], "snmp_timeout")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'examples/config.json').read_text())
        self.fixture = json.loads((ROOT / 'examples/discovery.synthetic.json').read_text())

    def test_all_api_pages_even_when_returned_offset_remains_zero(self):
        api = app.OpenNMS({}, fixture={})
        nodes = [{'id': str(i)} for i in range(1, 206)]
        def get(path, params):
            start = params['offset']
            page = nodes[start:start + params['limit']]
            return {'node': page, 'offset': 0, 'count': len(page), 'totalCount': len(nodes)}
        with patch.object(api, 'get', side_effect=get) as request:
            self.assertEqual(len(api.nodes()), 205)
            self.assertEqual([c.args[1]['offset'] for c in request.call_args_list], [0, 100, 200])

    def test_ignored_pagination_or_changing_counts_rejected(self):
        api = app.OpenNMS({}, fixture={})
        first = {'node': [{'id': str(i)} for i in range(1, 101)], 'offset': 0, 'count': 100, 'totalCount': 101}
        for second in [first, {'node': [{'id': '101'}], 'offset': 100, 'count': 1, 'totalCount': 102}, {'node': [], 'offset': 100, 'count': 0, 'totalCount': 101}]:
            with self.subTest(second=second['count']), patch.object(api, 'get', side_effect=[first, second]), self.assertRaises(app.InventoryError):
                api.nodes()

    def test_primary_interface_after_first_interface_page_is_found(self):
        api = app.OpenNMS({}, fixture={})
        interfaces = [{'id': str(i), 'snmpPrimary': 'N', 'ipAddress': f'192.0.2.{i}'} for i in range(1, 102)]
        interfaces[-1]['snmpPrimary'] = 'P'
        fixture = {'nodes': [{'id': '42', 'location': 'Branch Berlin'}], 'interfaces': {'42': interfaces}}
        api.fixture = fixture
        self.assertEqual(api.resolve(fixture['nodes'][0])['address'], '192.0.2.101')

    def test_duplicate_primary_refused_and_no_primary_reported(self):
        api = app.OpenNMS({}, fixture=self.fixture)
        self.assertIsNone(api.resolve(self.fixture['nodes'][2]))
        self.fixture['interfaces']['42'].append({'id': '999', 'snmpPrimary': 'P', 'ipAddress': '192.0.2.99'})
        with self.assertRaises(app.InventoryError):
            api.resolve(self.fixture['nodes'][0])

    def test_all_discovered_hosts_are_reported_without_netbox_or_manual_list(self):
        del self.config['netbox']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app.save_json(root / 'config.json', self.config)
            app.save_json(root / 'fixture.json', self.fixture)
            argv = ['inventory.py', '--config', str(root / 'config.json'), '--fixture', str(root / 'fixture.json'), '--state', str(root / 'state')]
            with patch.object(sys, 'argv', argv), patch.object(app.subprocess, 'run') as ssh, redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(), 0)
                ssh.assert_not_called()
            state = root / 'state/opennms-lab'
            report = app.load_json(state / 'inventory.json')
            self.assertEqual((report['discovered'], report['collected'], report['skipped']), (3, 2, 1))
            self.assertEqual(app.load_json(state / 'linux-branch.example.invalid--node-43.json')['source']['location'], 'Branch Berlin')
            self.assertIsNone(app.load_json(state / 'linux-branch.example.invalid--node-43.json')['target'])
            self.assertTrue(app.load_json(state / 'linux-branch.example.invalid--node-43.json')['synthetic'])

    def test_failed_discovery_preserves_previous_inventory_and_does_not_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app.save_json(root / 'config.json', self.config)
            previous = root / 'state/opennms-lab/inventory.json'
            app.save_json(previous, {'previous': True})
            argv = ['inventory.py', '--config', str(root / 'config.json'), '--state', str(root / 'state')]
            with patch.object(sys, 'argv', argv), patch.object(app.OpenNMS, '__init__', return_value=None), patch.object(app.OpenNMS, 'nodes', side_effect=app.InventoryError('failed page')), patch.object(app, 'walk') as walk:
                with self.assertRaises(app.InventoryError):
                    app.main()
                walk.assert_not_called()
            self.assertEqual(app.load_json(previous), {'previous': True})
            self.assertEqual(app.load_json(previous.parent / 'discovery.status.json')['enumeration'], 'failed')

    def test_netbox_resolves_identity_across_devices_and_vms(self):
        client = object.__new__(app.NetBox)
        client.config = self.config['netbox']
        hit = {'id': 123, 'custom_fields': {'opennms_node_key': 'opennms-lab:42'}}
        empty = {'count': 0, 'results': [], 'next': None}
        found = {'count': 1, 'results': [hit], 'next': None}
        with patch.object(client, 'api', side_effect=[empty, found]):
            self.assertEqual(client.resolve('opennms-lab', '42'), {'type': 'virtual-machines', 'id': 123})
        with patch.object(client, 'api', side_effect=[found, found]), self.assertRaises(app.InventoryError):
            client.resolve('opennms-lab', '42')
        with patch.object(client, 'api', side_effect=[empty, empty]):
            self.assertIsNone(client.resolve('opennms-lab', '42'))

    def test_invalid_netbox_filter_results_refused(self):
        client = object.__new__(app.NetBox)
        client.config = self.config['netbox']
        data = {'count': 1, 'results': [{'id': 999, 'custom_fields': {'opennms_node_key': 'someone-else:42'}}]}
        with patch.object(client, 'api', return_value=data), self.assertRaises(app.InventoryError):
            client.resolve('opennms-lab', '42')


class ConfigurationTests(unittest.TestCase):
    def test_missing_fields_named(self):
        for config, expected in [({}, "source_id"), ({"source_id": "ccq", "ssh_target": "karaf"}, "opennms"), ({"source_id": "ccq", "ssh_target": "karaf", "opennms": {}}, "opennms.url")]:
            with self.subTest(expected=expected), self.assertRaisesRegex(app.InventoryError, expected):
                app.validate(config)

    def test_wrong_section_type_named(self):
        config = {"source_id": "ccq", "ssh_target": "karaf", "opennms": "https://opennms.example.org"}
        with self.assertRaisesRegex(app.InventoryError, "opennms must be a JSON object"):
            app.validate(config)

    def test_legacy_netbox_field_defaults(self):
        config = json.loads((ROOT / 'examples/config.json').read_text())
        del config['netbox']['identity_field']
        app.validate(config)
        self.assertEqual(config['netbox']['identity_field'], 'opennms_node_key')

    def test_check_config_uses_neither_credentials_nor_network(self):
        argv = ['inventory.py', '--config', str(ROOT / 'examples/config.opennms-only.json'), '--check-config']
        with patch.object(sys, 'argv', argv), patch.object(app, 'OpenNMS') as api, redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(), 0)
            api.assert_not_called()


class InteractiveSshTests(unittest.TestCase):
    def test_password_mode_is_noninteractive_for_each_host(self):
        config = {'ssh_target': 'opennms-karaf', '_ssh_password_env': {'SSH_ASKPASS_REQUIRE': 'force'}}
        node = {'address': '192.0.2.42', 'location': 'Default'}
        text = (ROOT / 'examples/walk.txt').read_text()
        def run(argv, **kwargs):
            self.assertIn('BatchMode=no', argv)
            self.assertIn('NumberOfPasswordPrompts=1', argv)
            self.assertIn('ControlMaster=no', argv)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(kwargs['env'], config['_ssh_password_env'])
            kwargs['stdout'].write(text.encode())
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(app.subprocess, 'run', side_effect=run):
            self.assertEqual(len(app.walk(config, node, 10)), 5)

    def test_password_asked_once_and_delivered_to_multiple_real_helpers(self):
        config = {'ssh_target': 'opennms-karaf'}
        secret = 'test-only-p@ss with spaces'
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(app.getpass, 'getpass', return_value=secret) as prompt:
            with ExitStack() as stack:
                try:
                    stack.enter_context(app.ssh_session(config, True))
                except PermissionError:
                    self.skipTest('Execution sandbox blocks binding a local Unix socket; run this test on the target host')
                env = config['_ssh_password_env']
                helper = Path(env['SSH_ASKPASS'])
                endpoint = Path(env['OPENNMS_ASKPASS_SOCKET'])
                self.assertEqual(helper.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(endpoint.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(secret, helper.read_text())
                self.assertNotIn(secret, env.values())
                for _ in range(2):
                    reply = subprocess.run([sys.executable, str(helper), 'Password:'], env=env, capture_output=True, check=True, timeout=5)
                    self.assertEqual(reply.stdout, (secret + '\n').encode())
                denied = subprocess.run([sys.executable, str(helper), 'Trust this host key?'], env=env, capture_output=True, timeout=5)
                self.assertNotEqual(denied.returncode, 0)
            prompt.assert_called_once()
            self.assertNotIn('_ssh_password_env', config)
            self.assertFalse(helper.exists())
            self.assertFalse(endpoint.exists())

    def test_interactive_requires_terminal(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), self.assertRaises(app.InventoryError):
            with app.ssh_session({'ssh_target': 'opennms-karaf'}, True):
                self.fail('must reject noninteractive session')

    def test_agent_timeout_is_classified_despite_successful_ssh(self):
        config = {'ssh_target': 'opennms-karaf'}
        node = {'node_id': '3', 'address': '149.13.33.1', 'location': 'Default'}
        def run(argv, **kwargs):
            kwargs['stdout'].write(b" 149.13.33.1: org.opennms.netmgt.snmp.SnmpAgentTimeoutException: Timeout retrieving 'snmp:walk' for 149.13.33.1.\n")
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(app.subprocess, 'run', side_effect=run), self.assertRaises(app.SnmpTimeout):
            app.walk(config, node, 10)

    def test_auth_failure_is_distinct_from_host_timeout(self):
        def run(argv, **kwargs):
            kwargs['stderr'].write(b'Permission denied (keyboard-interactive,password)')
            return subprocess.CompletedProcess(argv, 255)
        with patch.object(app.subprocess, 'run', side_effect=run), self.assertRaises(app.SshAuthenticationError):
            app.walk({'ssh_target': 'opennms-karaf'}, {'address': '192.0.2.3', 'location': 'Default'}, 10)


class WalkDiagnosticTests(unittest.TestCase):
    def test_actual_rejected_output_is_saved_privately_without_weakening_parser(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {'ssh_target': 'opennms-karaf', '_walk_output_dir': temp}
            node = {'node_id': '3', 'address': '192.0.2.3', 'location': 'Default'}
            actual = b'Unexpected Karaf output\n'
            def run(argv, **kwargs):
                kwargs['stdout'].write(actual)
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(app.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(app.InventoryError, 'raw output saved to'):
                    app.walk(config, node, 10)
            capture = Path(temp) / 'node-3.txt'
            self.assertEqual(capture.read_bytes(), actual)
            self.assertEqual(capture.stat().st_mode & 0o777, 0o600)


class HostnameFileTests(unittest.TestCase):
    def test_readable_name_and_duplicate_labels_remain_unique(self):
        self.assertEqual(app.node_file_stem({'node_id': '3', 'label': 'web.example.org'}), 'web.example.org--node-3')
        self.assertNotEqual(app.node_file_stem({'node_id': '3', 'label': 'web'}), app.node_file_stem({'node_id': '4', 'label': 'web'}))

    def test_untrusted_label_cannot_escape_output_directory(self):
        for label in ['../../etc/passwd', '/tmp/something', '..', '', None, 'a' * 500, 'weird\nname']:
            with self.subTest(label=label):
                stem = app.node_file_stem({'node_id': '3', 'label': label})
                self.assertNotIn('/', stem)
                self.assertNotIn('\n', stem)
                self.assertFalse(stem.startswith('.'))
                self.assertLess(len(stem), 150)
        self.assertEqual(app.node_file_stem({'node_id': '3'}), 'node-3')


if __name__ == "__main__":
    unittest.main()
