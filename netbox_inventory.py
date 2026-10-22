#!/usr/bin/env python3
"""Import existing inventory, review NetBox bindings, sync and inspect history."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlencode

import inventory as core
from inventory_store import History, normalized, timestamp, utcnow

OBJECTS = [('devices', '/api/dcim/devices/'), ('virtual-machines', '/api/virtualization/virtual-machines/')]
MODELS = {'dcim.device', 'virtualization.virtualmachine'}


def pages(client, path, params=None):
    offset, expected, seen = 0, None, set()
    while True:
        data = client.api('GET', path + '?' + urlencode(dict(params or {}, limit=100, offset=offset, ordering='id')))
        rows, count = data.get('results'), data.get('count')
        if not isinstance(rows, list) or type(count) is not int or not 0 <= count <= 100000:
            raise core.InventoryError('Unsupported NetBox pagination contract')
        if expected is None:
            expected = count
        if expected != count or offset + len(rows) > count or len(rows) > 100:
            raise core.InventoryError('NetBox inventory changed during pagination; retry')
        for row in rows:
            identifier = row.get('id')
            if type(identifier) is not int or identifier < 1 or identifier in seen:
                raise core.InventoryError('Duplicate/invalid NetBox page identity')
            seen.add(identifier)
            yield row
        offset += len(rows)
        if offset == count:
            return
        if not rows:
            raise core.InventoryError('Incomplete NetBox pagination')


def endpoint(config):
    return config['netbox']['url'].rstrip('/')


def field_specs(config):
    settings = config['netbox']
    names = [settings['identity_field'], settings['field'], 'software_inventory_observed_at', 'software_inventory_status']
    if len(set(names)) != len(names):
        raise core.InventoryError('NetBox custom field names must be distinct')
    return [
        {'name': settings['identity_field'], 'type': 'text', 'label': 'OpenNMS node identity', 'filter_logic': 'exact'},
        {'name': settings['field'], 'type': 'json', 'label': 'Observed software inventory', 'default': {}},
        {'name': 'software_inventory_observed_at', 'type': 'datetime', 'label': 'Software last successfully observed'},
        {'name': 'software_inventory_status', 'type': 'text', 'label': 'Software inventory collection status'},
    ]


def ensure_fields(client, config, apply=False):
    actions = []
    for spec in field_specs(config):
        found = list(pages(client, '/api/extras/custom-fields/', {'name': spec['name']}))
        if len(found) > 1:
            raise core.InventoryError('Multiple custom fields with the same name')
        if found:
            field = found[0]
            kind = field['type'].get('value') if isinstance(field['type'], dict) else field['type']
            models = set(field.get('object_types', []))
            if kind != spec['type'] or not MODELS.issubset(models):
                raise core.InventoryError(f"Existing custom field {spec['name']} has incompatible type/object types")
            logic = field.get('filter_logic', {})
            logic = logic.get('value') if isinstance(logic, dict) else logic
            if spec.get('filter_logic') and logic != spec['filter_logic']:
                raise core.InventoryError('Identity field must use exact filtering')
            actions.append({'name': spec['name'], 'action': 'exists'})
        else:
            if apply:
                client.api('POST', '/api/extras/custom-fields/', dict(spec, object_types=sorted(MODELS), required=False, group_name='OpenNMS software inventory'))
            actions.append({'name': spec['name'], 'action': 'created' if apply else 'would_create'})
    return actions


def import_existing(db, config, state):
    report = core.load_json(state / 'inventory.json')
    if report['source_id'] != config['source_id'] or report.get('enumeration') != 'complete' or report.get('stopped'):
        raise core.InventoryError('Inventory report source does not match or run is incomplete')
    imported, unchanged = 0, 0
    snapshots = []
    statuses = {str(row['node_id']): row for row in report['nodes']}
    # First validate references for successful observations in this run.
    for status in report['nodes']:
        if status.get('collection') != 'complete':
            continue
        name = status.get('snapshot_file')
        if not isinstance(name, str) or Path(name).name != name:
            raise core.InventoryError('Missing/unsafe snapshot filename in inventory report')
        path = state / name
        if path.resolve().parent != state.resolve():
            raise core.InventoryError('Snapshot resolves outside the state directory')
        snap = core.load_json(path)
        if (snap['source']['id'], str(snap['source']['node_id'])) != (config['source_id'], str(status['node_id'])):
            raise core.InventoryError('Snapshot identity does not match inventory report')
        if timestamp(snap['observed_at']) != timestamp(status['observed_at']):
            raise core.InventoryError('Snapshot/report observation times differ; retry after collection finishes')
    # Also import retained last-good files for currently failed/unreachable hosts,
    # and older renamed files, while excluding nodes absent from the current run.
    for path in state.glob('*.json'):
        if path.resolve().parent != state.resolve():
            raise core.InventoryError('JSON file resolves outside the state directory')
        data = core.load_json(path)
        if not isinstance(data, dict) or data.get('collection_status') != 'complete_nonempty':
            continue
        identity = data.get('source', {})
        if identity.get('id') != config['source_id']:
            raise core.InventoryError('Snapshot in the source directory belongs to another OpenNMS source')
        if str(identity.get('node_id')) not in statuses:
            continue
        normalized(data)
        snapshots.append(data)
    for snap in snapshots:
        if db.observation(snap):
            imported += 1
        else:
            unchanged += 1
    db.report(report)
    return {'new_observations': imported, 'already_imported': unchanged, 'reported_nodes': len(report['nodes'])}


def name_key(value):
    return value.strip().rstrip('.').casefold() if isinstance(value, str) else ''


def address(value):
    try:
        return str(ipaddress.ip_interface(value).ip)
    except (ValueError, TypeError):
        return None


def nested_id(value):
    return value.get('id') if isinstance(value, dict) else None


def target_key(target):
    return target['type'], target['id']


def propose(db, client, config):
    objects, names, ips, dns = {}, {}, {}, {}
    owners = {(row["object_type"], row["object_id"]): {"node": row["node"], "source": row["source"]} for row in db.db.execute("SELECT * FROM bindings WHERE endpoint=?", (endpoint(config),))}
    for kind, path in OBJECTS:
        for record in pages(client, path):
            target = {'type': kind, 'id': record['id'], 'name': record['name'],
                      'site_id': nested_id(record.get('site')), 'tenant_id': nested_id(record.get('tenant')),
                      'site': (record.get('site') or {}).get('name'), 'tenant': (record.get('tenant') or {}).get('name'),
                      'identity_key': record.get('custom_fields', {}).get(config['netbox']['identity_field']),
                      'bound_node_id': owners.get((kind, record['id']), {}).get('node'),
                      'bound_source_id': owners.get((kind, record['id']), {}).get('source'), 'addresses': [], 'dns_names': []}
            key = target_key(target)
            objects[key] = target
            names.setdefault(name_key(record['name']), set()).add(key)
    for record in pages(client, '/api/ipam/ip-addresses/'):
        assigned = record.get('assigned_object') or {}
        device, vm = assigned.get('device'), assigned.get('virtual_machine')
        if record.get('assigned_object_type') == 'dcim.interface' and device:
            key = 'devices', device['id']
        elif record.get('assigned_object_type') == 'virtualization.vminterface' and vm:
            key = 'virtual-machines', vm['id']
        else:
            continue
        if key in objects:
            ip = address(record.get('address'))
            vrf = nested_id(record.get('vrf'))
            if ip:
                ips.setdefault(ip, []).append((key, vrf))
                objects[key]['addresses'].append({'address': ip, 'vrf_id': vrf, 'vrf': (record.get('vrf') or {}).get('name')})
            hostname = name_key(record.get('dns_name'))
            if hostname:
                dns.setdefault(hostname, []).append((key, vrf))
                objects[key]['dns_names'].append(hostname)
    plan = {'schema_version': 1, 'created_at': utcnow(), 'source_id': config['source_id'], 'netbox_url': endpoint(config), 'nodes': []}
    scope_map = config['netbox'].get('location_scope', {})
    for host in db.latest(config['source_id'], software=False):
        snap = host['snapshot']
        source, metadata = snap['source'], snap.get('host', {})
        node_id = str(source['node_id'])
        label, ip = metadata.get('label'), address(source.get('address'))
        scope = scope_map.get(source['location'])
        source_names = {name_key(value) for value in (label, metadata.get('sys_name')) if name_key(value)}
        name_matches = set().union(*(names.get(name, set()) for name in source_names))
        dns_matches = {key for name in source_names for key, vrf in dns.get(name, []) if scope is None or 'vrf_id' not in scope or vrf == scope['vrf_id']}
        ip_matches = {key for key, vrf in ips.get(ip, []) if scope is None or 'vrf_id' not in scope or vrf == scope['vrf_id']}
        candidates = []
        for key in sorted(name_matches | ip_matches | dns_matches):
            target = objects[key]
            if scope is not None and any(target.get(k) != scope[k] for k in ('site_id', 'tenant_id') if k in scope):
                continue
            candidates.append(dict(target, addresses=sorted(target['addresses'], key=lambda item: (item['address'], item['vrf_id'] or 0)), dns_names=sorted(set(target['dns_names'])), matched_name=key in name_matches, matched_ip=key in ip_matches, matched_dns=key in dns_matches))
        binding = db.binding(config['source_id'], node_id, endpoint(config))
        plan['nodes'].append({'node_id': node_id, 'label': label, 'address': ip, 'location': source['location'],
                              'observed_at': snap['observed_at'], 'collection_status': host['status'].get('collection'),
                              'source_metadata': metadata, 'synthetic': snap.get('synthetic', False),
                              'warning': None if scope is not None else 'No location scope configured; review site/tenant/VRF',
                              'existing_binding': binding, 'candidates': candidates,
                              'target': {'type': candidates[0]['type'], 'id': candidates[0]['id']} if len(candidates) == 1 else None,
                              'approve': False})
    return plan


def confirm(db, client, config, plan, apply=False):
    if (plan.get('schema_version'), plan.get('source_id'), plan.get('netbox_url')) != (1, config['source_id'], endpoint(config)):
        raise core.InventoryError('Mapping plan belongs to a different source/NetBox endpoint')
    fresh = {row['node_id']: row for row in propose(db, client, config)['nodes']}
    selected, seen_nodes, seen_targets = [], set(), set()
    for row in plan['nodes']:
        if row.get('approve') is not True:
            continue
        node = str(row['node_id'])
        if node in seen_nodes or node not in fresh or fresh[node]['synthetic']:
            raise core.InventoryError('Invalid, duplicate, synthetic or no-longer-present node in mapping plan')
        seen_nodes.add(node)
        latest = fresh[node]
        for key in ('label', 'address', 'location', 'source_metadata'):
            if row.get(key) != latest.get(key):
                raise core.InventoryError('Host identity evidence changed; generate and review a new plan')
        target = row['target']
        key = target_key(target)
        if key in seen_targets or key not in {target_key(c) for c in latest['candidates']}:
            raise core.InventoryError('Duplicate target or candidate no longer matches the live NetBox inventory')
        original_candidates = {target_key(c): c for c in row.get('candidates', [])}
        current_candidates = {target_key(c): c for c in latest['candidates']}
        if original_candidates.get(key) != current_candidates[key]:
            raise core.InventoryError('NetBox candidate evidence changed; generate and review a new plan')
        candidate = current_candidates[key]
        if candidate.get('identity_key') not in (None, '', f'{config["source_id"]}:{node}') or candidate.get('bound_node_id') not in (None, node) or candidate.get('bound_source_id') not in (None, config['source_id']):
            raise core.InventoryError('Candidate is already bound to another source/node')
        seen_targets.add(key)
        selected.append(dict(latest, target={'type': key[0], 'id': key[1]}))
    if not selected:
        raise core.InventoryError('No entries with approve=true selected')
    if apply:
        db.bind_many(config['source_id'], endpoint(config), selected)
    return {'bindings': len(selected), 'action': 'saved locally' if apply else 'would save locally; NetBox is unchanged'}


def sync_one(db, client, config, host, apply=False):
    snap, status = host['snapshot'], host['status']
    source, node = config['source_id'], str(snap['source']['node_id'])
    if snap.get('synthetic'):
        raise core.InventoryError('Synthetic observations cannot be exported to NetBox')
    binding = db.binding(source, node, endpoint(config))
    if not binding:
        raise core.InventoryError('No confirmed local binding; review the mapping plan first')
    original = json.loads(binding['evidence']).get('source_metadata', {})
    current = snap.get('host', {})
    for key in ('foreign_source', 'foreign_id', 'node_created_at'):
        if original.get(key) is not None and original[key] != current.get(key):
            raise core.InventoryError('OpenNMS identity evidence changed; binding needs review')
    target = {'type': binding['object_type'], 'id': binding['object_id']}
    record = client.request('GET', target)
    identity = f'{source}:{node}'
    existing = client.resolve(source, node)
    if existing and target_key(existing) != target_key(target):
        raise core.InventoryError('OpenNMS identity is already assigned to another NetBox object')
    fields = record.get('custom_fields', {})
    if fields.get(config['netbox']['identity_field']) not in (None, '', identity):
        raise core.InventoryError('Target NetBox object belongs to another OpenNMS node')
    old = fields.get(config['netbox']['field'])
    if old and (not isinstance(old, dict) or (old.get('source', {}).get('id'), str(old.get('source', {}).get('node_id'))) != (source, node)):
        raise core.InventoryError('Existing software field belongs to another source')
    observed = timestamp(snap['observed_at'])
    prior = fields.get('software_inventory_observed_at') or (old.get('observed_at') if isinstance(old, dict) else None)
    if prior and timestamp(prior) > observed:
        raise core.InventoryError('Refusing to replace a newer NetBox observation with an older local one')
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed)).total_seconds()
    stale_hours = config['netbox'].get('stale_after_hours', 24)
    if type(stale_hours) not in (int, float) or stale_hours <= 0:
        raise core.InventoryError('stale_after_hours must be a positive number')
    health = 'source_missing' if not host.get('present', True) else 'collection_failed' if status.get('collection') in ('failed', 'skipped') else 'stale' if age > stale_hours * 3600 else 'current'
    payload = {k: snap[k] for k in ('schema_version', 'source', 'host', 'content_sha256', 'software') if k in snap}
    wanted = {config['netbox']['identity_field']: identity, config['netbox']['field']: payload,
              'software_inventory_observed_at': observed, 'software_inventory_status': health}
    changes = {}
    for key, value in wanted.items():
        actual = fields.get(key)
        if key == 'software_inventory_observed_at' and actual:
            actual = timestamp(actual)
        if actual != value:
            changes[key] = value
    if not changes:
        return {'node_id': node, 'action': 'unchanged', 'fields': []}
    if not apply:
        return {'node_id': node, 'action': 'would_update', 'target': target, 'fields': list(changes)}
    client.request('PATCH', target, {'custom_fields': changes, 'changelog_message': f'OpenNMS software inventory {identity}'})
    actual = client.request('GET', target).get('custom_fields', {})
    for key, value in wanted.items():
        got = actual.get(key)
        if key == 'software_inventory_observed_at' and got:
            got = timestamp(got)
        if got != value:
            raise core.InventoryError('NetBox write not confirmed; rerun to reconcile')
    return {'node_id': node, 'action': 'confirmed', 'target': target, 'fields': list(changes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='OpenNMS NetBox inventory manager 3.1')
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', default='state')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('import', help='Import existing collector results without network access')
    setup = sub.add_parser('setup-fields', help='Review/create NetBox custom fields')
    setup.add_argument('--apply', action='store_true')
    propose_parser = sub.add_parser('propose', help='Read NetBox and produce an unapproved mapping plan')
    propose_parser.add_argument('--output', required=True)
    confirmation = sub.add_parser('confirm', help='Validate a reviewed plan and store approved bindings locally')
    confirmation.add_argument('--plan', required=True)
    confirmation.add_argument('--apply', action='store_true')
    syncing = sub.add_parser('sync', help='Export latest observations through confirmed bindings')
    syncing.add_argument('--apply', action='store_true')
    timeline = sub.add_parser('history', help='Show observed software version transitions')
    timeline.add_argument('--node', required=True)
    backup = sub.add_parser('backup', help='Create a consistent SQLite backup')
    backup.add_argument('--output', required=True)
    args = parser.parse_args()
    config = core.load_json(args.config)
    core.validate(config)
    state = Path(args.state) / config['source_id']
    state.mkdir(parents=True, exist_ok=True)
    with (state / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise core.InventoryError('Collector or inventory manager is running; retry after it finishes') from None
        with History(state / 'history.sqlite3') as db:
            if args.command == 'import':
                result = import_existing(db, config, state)
            elif args.command == 'history':
                result = db.timeline(config['source_id'], args.node)
            elif args.command == 'backup':
                db.backup(args.output)
                result = {'backup': args.output}
            else:
                if 'netbox' not in config:
                    raise core.InventoryError('Add NetBox connection settings to config.json first')
                client = core.NetBox(config['netbox'])
                if args.command == 'setup-fields':
                    result = ensure_fields(client, config, args.apply)
                elif args.command == 'propose':
                    result = propose(db, client, config)
                    core.save_json(args.output, result)
                    result = {'plan': args.output, 'nodes': len(result['nodes']), 'approved': 0}
                elif args.command == 'confirm':
                    result = confirm(db, client, config, core.load_json(args.plan), args.apply)
                else:
                    missing = [x['name'] for x in ensure_fields(client, config) if x['action'] != 'exists']
                    if missing:
                        raise core.InventoryError('Missing NetBox fields; run setup-fields first')
                    result, failed = [], False
                    for host in db.latest(config['source_id'], include_absent=True):
                        node = str(host['snapshot']['source']['node_id'])
                        if not host['present'] and not db.binding(config['source_id'], node, endpoint(config)):
                            continue
                        try:
                            item = sync_one(db, client, config, host, args.apply)
                        except (core.InventoryError, ValueError, KeyError, TypeError, OSError) as exc:
                            failed = True
                            item = {'node_id': node, 'action': 'failed', 'error': str(exc) if isinstance(exc, (core.InventoryError, ValueError)) else type(exc).__name__}
                        db.sync_result(config['source_id'], node, endpoint(config), item['action'], json.dumps(item))
                        result.append(item)
                    core.save_json(state / 'netbox-sync.json', {'attempted_at': utcnow(), 'apply': args.apply, 'results': result})
                    if failed:
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 1
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (core.InventoryError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, (core.InventoryError, ValueError, sqlite3.OperationalError)) else 'Binding conflict: NetBox target already assigned; local binding transaction rolled back' if isinstance(exc, sqlite3.IntegrityError) else f'Inventory operation failed: {type(exc).__name__}'
        print(message, file=sys.stderr)
        sys.exit(2)
