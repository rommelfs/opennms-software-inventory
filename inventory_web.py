#!/usr/bin/env python3
"""Local operator interface for OpenNMS inventory and NetBox matching."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import traceback
from urllib.parse import parse_qs, urlsplit

import inventory as core
from inventory_store import History
import netbox_inventory as manager

ASSETS = Path(__file__).resolve().parent / 'web'


def candidate_available(config, row, candidate):
    return (candidate.get('identity_key') in (None, '', f'{config["source_id"]}:{row["node_id"]}')
            and candidate.get('bound_node_id') in (None, row['node_id'])
            and candidate.get('bound_source_id') in (None, config['source_id']))


def eligible_rows(config, plan):
    """Bulk selection is a draft, never an implicit binding or remote write."""
    uses = {}
    for row in plan['nodes']:
        for candidate in row['candidates']:
            key = manager.target_key(candidate)
            uses.setdefault(key, set()).add(row['node_id'])
    result = []
    for row in plan['nodes']:
        if row['existing_binding'] or row['synthetic'] or len(row['candidates']) != 1:
            continue
        candidate = row['candidates'][0]
        try:
            namespace_known = bool(config['netbox'].get('location_scope', {}).get(row['location'])) or ipaddress.ip_address(row['address']).is_global
        except ValueError:
            namespace_known = False
        if (namespace_known and candidate['matched_ip'] and (candidate['matched_name'] or candidate.get('matched_dns'))
                and candidate_available(config, row, candidate) and len(uses[manager.target_key(candidate)]) == 1):
            result.append(row['node_id'])
    return result


class Application:
    def __init__(self, config, state, client_factory=None, demo=False):
        self.config, self.state, self.demo = config, Path(state), demo
        self.state.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory or (lambda: core.NetBox(config['netbox']))
        self.token = secrets.token_urlsafe(32)
        self.plan = None
        self.startup_message = None
        self.sync_preview = None
        self.jobs, self.job_lock, self.data_lock = {}, threading.Lock(), threading.RLock()
        self.active = None

    def locked(self, operation):
        with (self.state / '.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise core.InventoryError('Collection or another matching operation is running. Please try again when it finishes.') from None
            with History(self.state / 'history.sqlite3') as db:
                return operation(db)

    def state_data(self):
        with History(self.state / 'history.sqlite3') as db:
            hosts = db.latest(self.config['source_id'], software=False)
            status_rows = db.db.execute('SELECT node,status FROM hosts WHERE source=? AND present=1 ORDER BY CAST(node AS INTEGER)', (self.config['source_id'],)).fetchall()
            statuses = {row['node']: json.loads(row['status']) if row['status'] else {} for row in status_rows}
            bindings = {row['node']: dict(row) for row in db.db.execute('SELECT * FROM bindings WHERE source=? AND endpoint=?', (self.config['source_id'], manager.endpoint(self.config)))} if 'netbox' in self.config else {}
        with self.data_lock:
            plan = copy.deepcopy(self.plan)
        proposed = {row['node_id']: row for row in plan['nodes']} if plan else {}
        rows = []
        for host in hosts:
            snap = host['snapshot']
            identity = str(snap['source']['node_id'])
            row = proposed.get(identity, {'node_id': identity, 'label': snap.get('host', {}).get('label'), 'address': snap['source']['address'], 'location': snap['source']['location'], 'observed_at': snap['observed_at'], 'candidates': [], 'synthetic': snap.get('synthetic', False), 'warning': None})
            # SQLite binding state is authoritative after a review, even before refresh.
            row['existing_binding'] = bindings.get(identity)
            row['collection_status'] = statuses.get(identity, {}).get('collection')
            row['package_count'] = snap['package_count']
            row['error'] = statuses.get(identity, {}).get('error')
            rows.append(row)
        known = {row['node_id'] for row in rows}
        for identity, status in statuses.items():
            if identity not in known:
                rows.append({'node_id': identity, 'label': status.get('label'), 'address': status.get('address'), 'location': status.get('location'), 'observed_at': None, 'candidates': [], 'package_count': 0, 'collection_status': status.get('collection'), 'error': status.get('error') or status.get('reason'), 'existing_binding': bindings.get(identity), 'no_snapshot': True, 'synthetic': self.demo})
        rows.sort(key=lambda row: int(row['node_id']))
        eligible = [node for node in eligible_rows(self.config, plan) if node not in bindings] if plan else []
        with self.job_lock:
            active = copy.deepcopy(self.jobs.get(self.active)) if self.active else None
        return {'source_id': self.config['source_id'], 'netbox_url': self.config.get('netbox', {}).get('url'), 'demo': self.demo,
                'nodes': rows, 'matched_at': plan['created_at'] if plan else None, 'bulk_eligible': eligible,
                'active_job': active, 'startup_message': self.startup_message, 'version': '3.1'}

    def refresh(self):
        if 'netbox' not in self.config:
            raise core.InventoryError('Please add the NetBox section to config.json on the server.')
        plan = self.locked(lambda db: manager.propose(db, self.client_factory(), self.config))
        with self.data_lock:
            self.plan = plan
        return {'nodes': len(plan['nodes']), 'message': 'NetBox candidates refreshed. NetBox was only read.'}

    def import_files(self):
        if self.demo:
            return {'message': 'The demo data has already been imported.'}
        result = self.locked(lambda db: manager.import_existing(db, self.config, self.state))
        self.startup_message = None
        with self.data_lock:
            self.plan = None
            self.sync_preview = None
        return dict(result, message='Existing collection results imported. Please refresh the candidates.')

    def save_bindings(self, selections):
        if self.demo:
            raise core.InventoryError('The demo does not save mappings. You can review selections and candidates.')
        with self.data_lock:
            if self.plan is None:
                raise core.InventoryError('Please refresh NetBox candidates first.')
            plan = copy.deepcopy(self.plan)
        if not isinstance(selections, list) or not selections or len(selections) > 10000:
            raise core.InventoryError('Please select at least one mapping.')
        chosen = {}
        for selection in selections:
            node = str(selection.get('node_id', ''))
            if node in chosen:
                raise core.InventoryError('A node was selected more than once.')
            chosen[node] = selection.get('target')
        found = set()
        for row in plan['nodes']:
            row['approve'] = row['node_id'] in chosen
            if row['approve']:
                if row['existing_binding']:
                    raise core.InventoryError('Existing mappings cannot be reassigned.')
                row['target'] = chosen[row['node_id']]
                if not isinstance(row['target'], dict) or not any(manager.target_key(c) == manager.target_key(row['target']) for c in row['candidates']):
                    raise core.InventoryError('The selected target is not among the displayed candidates.')
                found.add(row['node_id'])
        if found != set(chosen):
            raise core.InventoryError('A selected node is no longer in the preview.')
        result = self.locked(lambda db: manager.confirm(db, self.client_factory(), self.config, plan, apply=True))
        with self.data_lock:
            self.sync_preview = None
            assigned = {manager.target_key(target): node for node, target in chosen.items()}
            if self.plan:
                for row in self.plan['nodes']:
                    for candidate in row['candidates']:
                        owner = assigned.get(manager.target_key(candidate))
                        if owner:
                            candidate['bound_node_id'] = owner
                            candidate['bound_source_id'] = self.config['source_id']
        # Keep the read-only candidate snapshot for the remaining nodes.
        return dict(result, message=f'{result["bindings"]} mapping(s) saved locally. NetBox is unchanged.')

    def node_detail(self, node):
        if not re.fullmatch(r'[1-9][0-9]*', node):
            raise core.InventoryError('Invalid node ID.')
        with History(self.state / 'history.sqlite3') as db:
            host = next((h for h in db.latest(self.config['source_id'], include_absent=True, node=node) if str(h['snapshot']['source']['node_id']) == node), None)
            if not host:
                raise core.InventoryError('There is no successful software observation for this node yet.')
            return {'snapshot': host['snapshot'], 'status': host['status'], 'timeline': db.timeline(self.config['source_id'], node)}

    def sync(self, apply=False, preview_id=None):
        if self.demo:
            raise core.InventoryError('The demo does not export to NetBox.')
        if 'netbox' not in self.config:
            raise core.InventoryError('Please configure NetBox in config.json.')
        with self.data_lock:
            previous_preview = copy.deepcopy(self.sync_preview)
        if apply and (not previous_preview or not preview_id or previous_preview['id'] != preview_id):
            raise core.InventoryError('Please create a current export preview first.')
        def run(db):
            client = self.client_factory()
            missing = [row['name'] for row in manager.ensure_fields(client, self.config) if row['action'] != 'exists']
            if missing:
                raise core.InventoryError('NetBox fields are missing. Run setup-fields in the CLI once; see the documentation.')
            results = []
            hosts = [host for host in db.latest(self.config['source_id'], include_absent=True)
                     if db.binding(self.config['source_id'], str(host['snapshot']['source']['node_id']), manager.endpoint(self.config))]
            fingerprint = [{'node': str(host['snapshot']['source']['node_id']), 'observed_at': host['snapshot']['observed_at'],
                            'content': host['snapshot']['content_sha256'], 'status': host['status'], 'present': host['present'],
                            'binding': db.binding(self.config['source_id'], str(host['snapshot']['source']['node_id']), manager.endpoint(self.config))}
                           for host in hosts]
            if apply:
                if any(row['action'] == 'failed' for row in previous_preview['results']):
                    raise core.InventoryError('The preview contains errors. Please resolve them and create a new preview.')
                if fingerprint != previous_preview['fingerprint']:
                    raise core.InventoryError('Collection results or mappings have changed. Please create a new preview.')
                refreshed = []
                for host in hosts:
                    node = str(host['snapshot']['source']['node_id'])
                    try:
                        refreshed.append(manager.sync_one(db, client, self.config, host, apply=False))
                    except (core.InventoryError, ValueError, KeyError, TypeError, OSError):
                        raise core.InventoryError('The NetBox state has changed. Please create a new preview.') from None
                if refreshed != previous_preview['results']:
                    raise core.InventoryError('The NetBox state has changed. Please create a new preview.')
            for host in hosts:
                node = str(host['snapshot']['source']['node_id'])
                try:
                    result = manager.sync_one(db, client, self.config, host, apply=apply)
                except (core.InventoryError, ValueError, KeyError, TypeError, OSError) as exc:
                    result = {'node_id': node, 'action': 'failed', 'error': str(exc) if isinstance(exc, (core.InventoryError, ValueError)) else type(exc).__name__}
                db.sync_result(self.config['source_id'], node, manager.endpoint(self.config), result['action'], json.dumps(result))
                results.append(result)
            core.save_json(self.state / 'netbox-sync.json', {'attempted_at': manager.utcnow(), 'apply': apply, 'results': results})
            ticket = None if apply else secrets.token_urlsafe(24)
            with self.data_lock:
                self.sync_preview = None if apply else {'id': ticket, 'fingerprint': fingerprint, 'results': copy.deepcopy(results)}
            return {'results': results, 'preview_id': ticket, 'apply': apply, 'message': 'Export completed.' if apply else 'Export preview created. NetBox was only read.'}
        return self.locked(run)

    def start_job(self, action, body):
        functions = {'refresh': self.refresh, 'import': self.import_files,
                     'bind': lambda: self.save_bindings(body.get('selections')),
                     'sync-preview': lambda: self.sync(False), 'sync-apply': lambda: self.sync(True, body.get('preview_id'))}
        if action not in functions:
            raise core.InventoryError('Unknown action.')
        with self.job_lock:
            if self.active and self.jobs[self.active]['status'] == 'running':
                raise core.InventoryError('An operation is already running. Please wait.')
            identity = secrets.token_hex(12)
            self.jobs[identity] = {'id': identity, 'action': action, 'status': 'running'}
            self.active = identity
        def worker():
            try:
                result = functions[action]()
                update = {'status': 'done', 'result': result}
            except (core.InventoryError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
                update = {'status': 'failed', 'error': str(exc) if isinstance(exc, (core.InventoryError, ValueError)) else f'Operation failed ({type(exc).__name__}).'}
                if isinstance(exc, manager.MappingConflict):
                    update['conflicts'] = exc.conflicts
            except Exception:
                traceback.print_exc()
                update = {'status': 'failed', 'error': 'Internal error. Check the server logs for details.'}
            with self.job_lock:
                self.jobs[identity].update(update)
                if len(self.jobs) > 50:
                    for old in list(self.jobs):
                        if old != self.active:
                            self.jobs.pop(old)
                            break
        threading.Thread(target=worker, daemon=True).start()
        return {'job_id': identity}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'InventoryWeb/3.1'

        def log_message(self, *args):
            pass  # Never log bearer tokens, bodies or inventory records.

        def send(self, status, payload, content_type='application/json; charset=utf-8'):
            raw = json.dumps(payload, ensure_ascii=False).encode() if not isinstance(payload, bytes) else payload
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def permitted_host(self):
            host = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            return host in allowed and self.headers.get('Origin', 'http://' + host) in {'http://' + item for item in allowed}

        def authorized(self):
            return self.permitted_host() and secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + app.token).encode())

        def do_GET(self):
            if not self.permitted_host():
                self.send(403, {'error': 'Invalid Host or Origin.'})
                return
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/mapping.js': ('mapping.js', 'application/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in assets:
                name, kind = assets[path]
                self.send(200, (ASSETS / name).read_bytes(), kind)
                return
            if not self.authorized():
                self.send(401, {'error': 'Please open the access link printed when the server started.'})
                return
            try:
                if path == '/api/state':
                    self.send(200, app.state_data())
                elif path == '/api/node':
                    self.send(200, app.node_detail(parse_qs(urlsplit(self.path).query).get('id', [''])[0]))
                elif path.startswith('/api/jobs/'):
                    with app.job_lock:
                        job = copy.deepcopy(app.jobs.get(path.rsplit('/', 1)[1]))
                    self.send(200 if job else 404, job or {'error': 'Job not found.'})
                else:
                    self.send(404, {'error': 'Not found.'})
            except (core.InventoryError, ValueError, sqlite3.Error) as exc:
                self.send(409, {'error': str(exc)})

        def do_POST(self):
            if not self.authorized():
                self.send(403, {'error': 'Access denied.'})
                return
            try:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise core.InventoryError('Expected JSON.')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise core.InventoryError('Invalid request size.')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise core.InventoryError('Invalid request.')
                path = urlsplit(self.path).path
                if not path.startswith('/api/'):
                    raise core.InventoryError('Unknown action.')
                action = path[len('/api/'):]
                self.send(202, app.start_job(action, body))
            except (core.InventoryError, ValueError, TypeError) as exc:
                self.send(409, {'error': str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--state', default='state')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--demo', action='store_true', help='Use isolated synthetic data; no external requests or writes')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    missing_assets = [name for name in ('index.html', 'style.css', 'mapping.js', 'app.js') if not (ASSETS / name).is_file()]
    if missing_assets:
        raise core.InventoryError('Web files are missing. Please copy the entire web directory next to inventory_web.py: ' + ', '.join(missing_assets))
    temporary = tempfile.TemporaryDirectory(prefix='inventory-web-demo-') if args.demo else None
    try:
        if args.demo:
            from inventory_web_demo import create_demo
            app = create_demo(Path(temporary.name))
        else:
            if not args.config:
                parser.error('--config is required unless --demo is used')
            config = core.load_json(args.config)
            core.validate(config)
            app = Application(config, Path(args.state) / config['source_id'])
            if (app.state / 'inventory.json').exists():
                try:
                    app.import_files()
                except (core.InventoryError, ValueError) as exc:
                    app.startup_message = str(exc)
                    print('Startup import deferred: ' + str(exc), file=sys.stderr)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
        print(f'Inventory Web 3.1: http://127.0.0.1:{args.port}/#token={app.token}', flush=True)
        print('Run on the OpenNMS server and use the same local port in the SSH tunnel. Press Ctrl+C to stop the web interface.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if temporary:
            temporary.cleanup()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (core.InventoryError, ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
