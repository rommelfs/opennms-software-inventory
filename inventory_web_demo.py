"""Synthetic, isolated demonstration data. Never connects to external servers."""
import copy
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit

import inventory as core
from inventory_store import History
import netbox_inventory as manager


class DemoNetBox(core.NetBox):
    def __init__(self, config, objects, ips):
        self.config, self.objects, self.ips = config['netbox'], objects, ips

    def api(self, method, path, body=None):
        if method != 'GET':
            raise core.InventoryError('Demo: NetBox-Schreibzugriffe sind deaktiviert.')
        parsed, params = urlsplit(path), parse_qs(urlsplit(path).query)
        if parsed.path == '/api/ipam/ip-addresses/':
            rows = self.ips
        else:
            kind = 'devices' if '/dcim/devices/' in parsed.path else 'virtual-machines'
            rows = self.objects[kind]
            parts = parsed.path.strip('/').split('/')
            if len(parts) == 4:
                return copy.deepcopy(next(r for r in rows if r['id'] == int(parts[-1])))
            identity = 'cf_' + self.config['identity_field']
            if identity in params:
                rows = [r for r in rows if r['custom_fields'].get(self.config['identity_field']) == params[identity][0]]
        offset, limit = int(params.get('offset', ['0'])[0]), int(params.get('limit', ['100'])[0])
        return {'count': len(rows), 'results': copy.deepcopy(rows[offset:offset + limit]), 'next': '/next' if offset + limit < len(rows) else None}


def create_demo(state):
    from inventory_web import Application
    config = {
        'source_id': 'opennms-demo', 'ssh_target': 'unused-demo',
        'opennms': {'url': 'https://opennms.example.invalid/opennms'},
        'netbox': {'url': 'https://netbox.example.invalid', 'field': 'opennms_software_inventory',
                   'identity_field': 'opennms_node_key', 'location_scope': {
                       'Core / Frankfurt': {'site_id': 1}, 'Minion / Berlin': {'site_id': 2}
                   }}
    }
    records = [
        ('3', 'web-01.example.org', '192.0.2.11', 'Core / Frankfurt', 1),
        ('4', 'web-02.example.org', '192.0.2.12', 'Core / Frankfurt', 1),
        ('10', 'db-01.example.org', '192.0.2.21', 'Core / Frankfurt', 1),
        ('12', 'cache-01.example.org', '192.0.2.31', 'Minion / Berlin', 2),
        ('13', 'worker-01.example.org', '192.0.2.41', 'Minion / Berlin', 2),
        ('17', 'monitor-01.example.org', '192.0.2.51', 'Core / Frankfurt', 1),
        ('19', 'backup-01.example.org', '192.0.2.61', 'Minion / Berlin', 2),
        ('24', 'gateway-01.example.org', '192.0.2.71', 'Core / Frankfurt', 1),
    ]
    objects, ips, statuses = {'devices': [], 'virtual-machines': []}, [], []
    at = datetime.now(timezone.utc).replace(microsecond=0)
    with History(state / 'history.sqlite3') as db:
        for i, (node, name, ip, location, site) in enumerate(records):
            info = {'node_id': node, 'label': name, 'address': ip, 'location': location,
                    'foreign_source': 'Linux', 'foreign_id': node}
            versions = [('8.3.6-0ubuntu0.24.04.13', at - timedelta(days=2)), ('8.3.6-0ubuntu0.24.04.14', at)]
            for version, observed in versions:
                software = core.parse_walk(f'[{core.OID}].[1] = php8.3-fpm_{version}_amd64\n[{core.OID}].[2] = nginx_1.24.0-2ubuntu7_amd64\n[{core.OID}].[3] = openssl_3.0.13-0ubuntu3_amd64\n')
                snap = core.snapshot(config, info, software, observed.isoformat())
                snap.update(observed_at=observed.isoformat(), synthetic=True)
                db.observation(snap)
            statuses.append({'node_id': node, 'label': name, 'address': ip, 'location': location,
                             'attempted_at': at.isoformat(), 'collection': 'failed' if node == '19' else 'complete',
                             **({'error': 'SNMP timeout'} if node == '19' else {})})
            if node == '19':
                continue
            kind = 'devices' if node in ('10', '24') else 'virtual-machines'
            target = {'id': 100 + i, 'name': name if node != '12' else 'redis-berlin-01', 'custom_fields': {},
                      'site': {'id': site, 'name': 'Frankfurt' if site == 1 else 'Berlin'},
                      'tenant': {'id': 1, 'name': 'Infrastructure'}}
            objects[kind].append(target)
            ips.append({'id': 500 + i, 'address': ip + '/24', 'dns_name': name if node != '17' else '',
                        'vrf': None, 'assigned_object_type': 'dcim.interface' if kind == 'devices' else 'virtualization.vminterface',
                        'assigned_object': {'device' if kind == 'devices' else 'virtual_machine': {'id': target['id']}}})
            if node == '13':
                # Same DNS label on a second object, deliberately ambiguous.
                objects['devices'].append({'id': 901, 'name': name, 'site': {'id': 2, 'name': 'Berlin'}, 'tenant': {'id': 1, 'name': 'Infrastructure'}, 'custom_fields': {}})
            if node == '24':
                db.bind_many(config['source_id'], manager.endpoint(config), [{'node_id': node, 'target': {'type': kind, 'id': target['id']}, 'source_metadata': {}}])
                target['custom_fields']['opennms_node_key'] = config['source_id'] + ':' + node
        db.report({'source_id': config['source_id'], 'started_at': at.isoformat(), 'enumeration': 'complete', 'synthetic': True, 'nodes': statuses})
    app = Application(config, state, client_factory=lambda: DemoNetBox(config, objects, ips), demo=True)
    app.refresh()
    return app
