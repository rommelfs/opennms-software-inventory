"""Append-only observations and durable source/NetBox bindings (stdlib SQLite)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Observation timestamps must include a timezone')
    return parsed.astimezone(timezone.utc).isoformat()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def normalized(snap):
    source = snap['source']
    source_id, node_id = source['id'], str(source['node_id'])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', source_id) or not re.fullmatch(r'[1-9][0-9]*', node_id):
        raise ValueError('Invalid snapshot source/node identity')
    if source.get('system') != 'opennms' or snap.get('collection_status') != 'complete_nonempty':
        raise ValueError('Only complete OpenNMS snapshots can be imported')
    if snap.get('schema_version') not in (1, 2):
        raise ValueError('Unsupported snapshot schema')
    software = snap['software']
    if not isinstance(software, list) or not software or len(software) > 100000:
        raise ValueError('Invalid software list')
    for row in software:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k] for k in ('name', 'raw')):
            raise ValueError('Invalid software entry')
        for key in ('version', 'architecture'):
            if row.get(key) is not None and not isinstance(row[key], str):
                raise ValueError('Invalid software version/architecture')
    # Preserve the collector's deterministic software order and evidence digest.
    supplied = hashlib.sha256(json.dumps(software, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if snap.get('content_sha256') != supplied:
        raise ValueError('Snapshot content hash does not match its software list')
    content = json.dumps(software, sort_keys=True, ensure_ascii=False)
    return source_id, node_id, timestamp(snap['observed_at']), supplied, content


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError('Unsupported history database schema')
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS contents (
                hash TEXT PRIMARY KEY, software TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                source TEXT NOT NULL, node TEXT NOT NULL, observed_at TEXT NOT NULL,
                hash TEXT NOT NULL REFERENCES contents(hash), snapshot TEXT NOT NULL,
                PRIMARY KEY(source,node,observed_at)
            );
            CREATE TABLE IF NOT EXISTS hosts (
                source TEXT NOT NULL, node TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1,
                status_at TEXT, status TEXT, PRIMARY KEY(source,node)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                source TEXT NOT NULL, node TEXT NOT NULL, attempted_at TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(source,node,attempted_at)
            );
            CREATE TABLE IF NOT EXISTS runs (
                source TEXT NOT NULL, started_at TEXT NOT NULL, report TEXT NOT NULL,
                PRIMARY KEY(source,started_at)
            );
            CREATE TABLE IF NOT EXISTS bindings (
                source TEXT NOT NULL, node TEXT NOT NULL, endpoint TEXT NOT NULL,
                object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL, evidence TEXT NOT NULL,
                PRIMARY KEY(source,node,endpoint), UNIQUE(endpoint,object_type,object_id)
            );
            CREATE TABLE IF NOT EXISTS syncs (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, node TEXT NOT NULL,
                endpoint TEXT NOT NULL, attempted_at TEXT NOT NULL,
                outcome TEXT NOT NULL, detail TEXT NOT NULL
            );
            PRAGMA user_version=1;
        ''')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def observation(self, snap):
        source, node, observed, digest, content = normalized(snap)
        old = self.db.execute('SELECT hash FROM observations WHERE source=? AND node=? AND observed_at=?', (source, node, observed)).fetchone()
        if old and old['hash'] != digest:
            raise ValueError('Conflicting snapshots at the same observation time')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO contents VALUES (?,?)', (digest, content))
            result = self.db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)', (source, node, observed, digest, json.dumps(dict({key: value for key, value in snap.items() if key != 'software'}, package_count=len(snap['software'])), ensure_ascii=False)))
            self.db.execute('INSERT OR IGNORE INTO hosts(source,node) VALUES (?,?)', (source, node))
        return bool(result.rowcount)

    def attempt(self, source, status):
        node, attempted = str(status['node_id']), timestamp(status['attempted_at'])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO attempts VALUES (?,?,?,?)', (source, node, attempted, json.dumps(status, ensure_ascii=False)))
            self.db.execute('INSERT OR IGNORE INTO hosts(source,node) VALUES (?,?)', (source, node))
            self.db.execute('UPDATE hosts SET status_at=?,status=? WHERE source=? AND node=? AND (status_at IS NULL OR status_at<=?)', (attempted, json.dumps(status, ensure_ascii=False), source, node, attempted))

    def report(self, report):
        source = report['source_id']
        started = timestamp(report['started_at'])
        if report.get('enumeration') != 'complete' or report.get('stopped'):
            raise ValueError('Cannot use an incomplete run as the current host list')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?)', (source, started, json.dumps(report, ensure_ascii=False)))
            # Importing an old run must not undo a newer source membership view.
            latest = self.db.execute('SELECT MAX(started_at) FROM runs WHERE source=?', (source,)).fetchone()[0]
            if latest == started:
                self.db.execute('UPDATE hosts SET present=0 WHERE source=?', (source,))
            for status in report['nodes']:
                node, attempted = str(status['node_id']), timestamp(status['attempted_at'])
                self.db.execute('INSERT OR IGNORE INTO attempts VALUES (?,?,?,?)', (source, node, attempted, json.dumps(status, ensure_ascii=False)))
                self.db.execute('INSERT OR IGNORE INTO hosts(source,node) VALUES (?,?)', (source, node))
                if latest == started:
                    self.db.execute('UPDATE hosts SET present=1 WHERE source=? AND node=?', (source, node))
                self.db.execute('UPDATE hosts SET status_at=?,status=? WHERE source=? AND node=? AND (status_at IS NULL OR status_at<=?)', (attempted, json.dumps(status, ensure_ascii=False), source, node, attempted))

    def latest(self, source, include_absent=False, node=None, software=True):
        rows = self.db.execute('''SELECT o.*,h.present,h.status FROM observations o JOIN hosts h
            ON o.source=h.source AND o.node=h.node WHERE o.source=? AND (? IS NULL OR o.node=?)
            AND o.observed_at=(SELECT MAX(n.observed_at) FROM observations n WHERE n.source=o.source AND n.node=o.node)
            ORDER BY CAST(o.node AS INTEGER)''', (source, str(node) if node is not None else None, str(node) if node is not None else None)).fetchall()
        result = []
        for row in rows:
            if not row['present'] and not include_absent:
                continue
            snap = json.loads(row['snapshot'])
            if software and 'software' not in snap:
                snap['software'] = json.loads(self.db.execute('SELECT software FROM contents WHERE hash=?', (row['hash'],)).fetchone()[0])
            if not software:
                snap['package_count'] = snap.get('package_count', len(snap['software']) if 'software' in snap else None)
                if snap['package_count'] is None:
                    snap['package_count'] = len(json.loads(self.db.execute('SELECT software FROM contents WHERE hash=?', (row['hash'],)).fetchone()[0]))
                snap.pop('software', None)
            result.append(dict(snapshot=snap, status=json.loads(row['status']) if row['status'] else {}, present=bool(row['present'])))
        return result

    def bind_many(self, source, endpoint, bindings):
        with self.db:
            for binding in bindings:
                node, target = str(binding['node_id']), binding['target']
                if target['type'] not in ('devices', 'virtual-machines') or type(target['id']) is not int or target['id'] < 1:
                    raise ValueError('Invalid binding target')
                old = self.binding(source, node, endpoint)
                if old:
                    if (old['object_type'], old['object_id']) != (target['type'], target['id']):
                        raise ValueError('Existing binding cannot be reassigned implicitly')
                    continue
                self.db.execute('INSERT INTO bindings VALUES (?,?,?,?,?,?,?)', (source, node, endpoint, target['type'], target['id'], utcnow(), json.dumps(binding, ensure_ascii=False)))

    def binding(self, source, node, endpoint):
        row = self.db.execute('SELECT * FROM bindings WHERE source=? AND node=? AND endpoint=?', (source, str(node), endpoint)).fetchone()
        return dict(row) if row else None

    def sync_result(self, source, node, endpoint, outcome, detail):
        with self.db:
            self.db.execute('INSERT INTO syncs(source,node,endpoint,attempted_at,outcome,detail) VALUES (?,?,?,?,?,?)', (source, str(node), endpoint, utcnow(), outcome, detail))

    def timeline(self, source, node):
        rows = self.db.execute('SELECT * FROM observations WHERE source=? AND node=? ORDER BY observed_at', (source, str(node))).fetchall()
        history, previous, previous_hash = [], None, None
        for row in rows:
            software = json.loads(self.db.execute('SELECT software FROM contents WHERE hash=?', (row['hash'],)).fetchone()[0])
            if row['hash'] == previous_hash:
                history[-1]['last_observed_at'] = row['observed_at']
                history[-1]['observations'] += 1
                continue
            history.append({'first_observed_at': row['observed_at'], 'last_observed_at': row['observed_at'], 'observations': 1, 'hash': row['hash'], 'changes': diff(previous, software)})
            previous, previous_hash = software, row['hash']
        return history

    def backup(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise ValueError('Backup destination must be a new file')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
        finally:
            target.close()


def diff(previous, current):
    def index(rows):
        values = {}
        for row in rows:
            key = (row['name'], row.get('architecture'))
            values.setdefault(key, set()).add(row.get('version') if row.get('version') is not None else 'unknown:' + row['raw'])
        return values
    old, new = index(previous or []), index(current)
    changes = []
    for name, arch in sorted(set(old) | set(new), key=lambda item: (item[0], item[1] or '')):
        key = name, arch
        if old.get(key) == new.get(key):
            continue
        changes.append({'name': name, 'architecture': arch,
                        'kind': 'baseline' if previous is None else 'newly_observed' if key not in old else 'no_longer_observed' if key not in new else 'version_changed',
                        'before': sorted(old.get(key, [])), 'after': sorted(new.get(key, []))})
    return changes
