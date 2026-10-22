#!/usr/bin/env python3
"""Collect hrSWInstalledName through OpenNMS Karaf and project it into NetBox."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import sqlite3
import socketserver
import stat
import threading
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from inventory_store import History

OID = "1.3.6.1.2.1.25.6.3.1.2"
LIMIT = 8 * 1024 * 1024
ROW = re.compile(r"^\.*\[\.?" + re.escape(OID) + r"\]\.\[(\d+)\] = (.*)$")
PACKAGE = re.compile(r"^([a-z0-9][a-z0-9+.:-]*)_([0-9][A-Za-z0-9.+:~\-]*)_([A-Za-z0-9][A-Za-z0-9-]*)$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class InventoryError(Exception):
    pass


class SnmpTimeout(InventoryError):
    pass


class SshAuthenticationError(InventoryError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def node_file_stem(node):
    """Readable names with a stable ID suffix; never use raw labels as paths."""
    node_id = str(node["node_id"])
    if not re.fullmatch(r"[1-9][0-9]*", node_id):
        raise InventoryError("Invalid node ID for output filename")
    label = node.get("label")
    label = label if isinstance(label, str) else ""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip(".-_")[:120].rstrip(".-_")
    return f"{safe}--node-{node_id}" if safe else f"node-{node_id}"


def load_json(path):
    if Path(path).stat().st_size > LIMIT:
        raise InventoryError("JSON input exceeds size limit")
    return json.loads(Path(path).read_text())


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".inventory-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_url(value):
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise InventoryError("API URL must be HTTPS without credentials/query/fragment")


def validate_node(node):
    if not re.fullmatch(r"[1-9][0-9]*", str(node["node_id"])):
        raise InventoryError("Invalid OpenNMS node ID")
    if not re.fullmatch(r"[0-9A-Fa-f:.]+", node["address"]):
        raise InventoryError("Use an IPv4 or IPv6 literal without a zone identifier")
    ipaddress.ip_address(node["address"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. /-]{0,199}", node["location"]):
        raise InventoryError("Location contains unsupported characters")


def validate(config):
    if not isinstance(config, dict):
        raise InventoryError("Configuration must be a JSON object")
    if "nodes" in config:
        raise InventoryError("Remove the nodes list: hosts are discovered from OpenNMS")
    missing = [key for key in ("source_id", "ssh_target", "opennms") if key not in config]
    if missing:
        raise InventoryError("Missing configuration field(s): " + ", ".join(missing) + "; see examples/config.json (v2 automatic discovery)")
    for key in ("source_id", "ssh_target"):
        if not isinstance(config[key], str) or not config[key]:
            raise InventoryError(f"Configuration field {key} must be a nonempty string")
    for section in ("opennms", "netbox"):
        if section not in config:
            continue
        settings = config[section]
        if not isinstance(settings, dict):
            raise InventoryError(f"Configuration field {section} must be a JSON object")
        if "url" not in settings:
            raise InventoryError(f"Missing configuration field: {section}.url")
        if not isinstance(settings["url"], str) or not settings["url"]:
            raise InventoryError(f"Configuration field {section}.url must be a nonempty string")
        for key in ("username_env", "password_env", "token_env", "ca_file", "auth_scheme", "field", "identity_field"):
            if key in settings and (not isinstance(settings[key], str) or not settings[key]):
                raise InventoryError(f"Configuration field {section}.{key} must be a nonempty string")
    if "netbox" in config:
        config["netbox"].setdefault("field", "opennms_software_inventory")
        config["netbox"].setdefault("identity_field", "opennms_node_key")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config["source_id"]):
        raise InventoryError("Invalid source_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", config["ssh_target"]):
        raise InventoryError("Use a simple SSH config alias for ssh_target")
    validate_url(config["opennms"]["url"])
    if "netbox" in config:
        validate_url(config["netbox"]["url"])
        for key in ("field", "identity_field"):
            if not re.fullmatch(r"[a-z][a-z0-9_]*", config["netbox"][key]):
                raise InventoryError("Invalid NetBox custom field")
        if config["netbox"]["field"] == config["netbox"]["identity_field"]:
            raise InventoryError("Inventory and identity fields must be different")
        if config["netbox"].get("auth_scheme", "Token") not in {"Token", "Bearer"}:
            raise InventoryError("Unsupported NetBox authentication scheme")


class OpenNMS:
    """Enumerate all visible nodes, then resolve their primary SNMP interfaces."""
    def __init__(self, config, fixture=None):
        self.config, self.fixture = config, fixture
        if fixture is not None:
            return
        user = os.environ.get(config.get("username_env", "OPENNMS_USERNAME"), "")
        password = os.environ.get(config.get("password_env", "OPENNMS_PASSWORD"), "")
        if not user or ":" in user or not password:
            raise InventoryError("Missing OpenNMS API username/password environment variables")
        self.auth = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
        self.opener = build_opener(NoRedirect(), HTTPSHandler(
            context=ssl.create_default_context(cafile=config.get("ca_file"))))

    def get(self, path, params):
        if self.fixture is not None:
            if path == "/rest/nodes":
                rows, key = self.fixture["nodes"], "node"
            else:
                node_id = path.split("/")[3]
                rows, key = self.fixture["interfaces"][node_id], "ipInterface"
            offset, limit = params["offset"], params["limit"]
            page = rows[offset:offset + limit]
            return {key: page, "offset": offset, "count": len(page), "totalCount": len(rows)}
        url = self.config["url"].rstrip("/") + path + "?" + urlencode(params)
        req = Request(url, headers={"Authorization": self.auth, "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise InventoryError("OpenNMS API response exceeds size limit")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise InventoryError("Unexpected OpenNMS API response")
                return result
        except (HTTPError, URLError) as exc:
            raise InventoryError(f"OpenNMS API request failed (HTTP {getattr(exc, 'code', None) or 'network error'})") from None

    def pages(self, path, key):
        rows, offset, expected, seen = [], 0, None, set()
        while True:
            data = self.get(path, {"limit": 100, "offset": offset, "orderBy": "id", "order": "asc"})
            page, total = data.get(key, []), data.get("totalCount")
            if not isinstance(page, list) or type(total) is not int or not 0 <= total <= 100000:
                raise InventoryError("Unsupported OpenNMS pagination contract")
            # v1 services can leave the wrapper's offset at its default 0.
            # Advance our requested offset and detect repeated identities instead.
            if data.get("offset") not in (0, offset) or data.get("count") != len(page):
                raise InventoryError("Invalid OpenNMS page metadata")
            if expected is None:
                expected = total
            if total != expected or len(rows) + len(page) > total or len(page) > 100:
                raise InventoryError("OpenNMS inventory changed or pagination is inconsistent; retry")
            for record in page:
                if not isinstance(record, dict):
                    raise InventoryError("Invalid OpenNMS list record")
                identity = str(record.get("id", ""))
                if not re.fullmatch(r"[1-9][0-9]*", identity) or identity in seen:
                    raise InventoryError("Repeated/invalid OpenNMS page identity")
                seen.add(identity)
            rows.extend(page)
            if len(rows) == total:
                return rows
            if not page:
                raise InventoryError("Incomplete OpenNMS pagination")
            offset += len(page)

    def nodes(self):
        rows = self.pages("/rest/nodes", "node")
        seen = set()
        for row in rows:
            identity = str(row.get("id", ""))
            if not re.fullmatch(r"[1-9][0-9]*", identity) or identity in seen:
                raise InventoryError("Duplicate/invalid OpenNMS node identity")
            seen.add(identity)
        return rows

    def resolve(self, row):
        node_id = str(row["id"])
        interfaces = self.pages(f"/rest/nodes/{node_id}/ipinterfaces", "ipInterface")
        primary = [i for i in interfaces if i.get("snmpPrimary") == "P"]
        if not primary:
            return None
        if len(primary) != 1:
            raise InventoryError("Multiple primary SNMP interfaces; node not scanned")
        node = {"node_id": node_id, "address": primary[0]["ipAddress"],
                "location": row.get("location"), "label": row.get("label"),
                "foreign_source": row.get("foreignSource"), "foreign_id": row.get("foreignId"),
                "sys_description": row.get("sysDescription"), "sys_name": row.get("sysName"), "node_created_at": row.get("createTime")}
        validate_node(node)
        return node


def parse_walk(output):
    """OpenNMS WalkCommand prints rows only after its future succeeds in full.

    Reject unexpected output, even on SSH exit 0: WalkCommand catches exceptions.
    An empty result cannot distinguish absent MIB support from an empty inventory.
    """
    output = ANSI.sub("", output)
    rows, indices = {}, set()
    for line in output.splitlines():
        if not line.strip() or re.fullmatch(r"\.+", line.strip()):
            continue
        match = ROW.fullmatch(line)
        if not match:
            raise InventoryError("Unrecognized walk output; snapshot rejected")
        index, raw = match.groups()
        if index in indices or not raw.strip():
            raise InventoryError("Duplicate index or empty software name")
        indices.add(index)
        if len(indices) > 100000:
            raise InventoryError("Row limit exceeded")
        if raw not in rows:
            package = PACKAGE.fullmatch(raw)
            rows[raw] = {
                "raw": raw,
                "name": package[1] if package else raw,
                "version": package[2] if package else None,
                "architecture": package[3] if package else None,
                "parser": "debian-name_version_arch" if package else "unparsed",
                "observation": "installed",
            }
    if not rows:
        raise InventoryError("Empty/unsupported walk; previous snapshot retained")
    return [rows[key] for key in sorted(rows)]


ASKPASS_HELPER = """#!/usr/bin/env python3
import os
import socket
import sys
# Only answer password questions; never approve host keys or other challenges.
if len(sys.argv) < 2 or 'password' not in sys.argv[1].lower():
    sys.exit(1)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.settimeout(5)
    connection.connect(os.environ['OPENNMS_ASKPASS_SOCKET'])
    chunks = []
    while True:
        part = connection.recv(4096)
        if not part:
            break
        chunks.append(part)
    sys.stdout.buffer.write(b''.join(chunks))
"""


class PasswordHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(5)
            self.request.sendall(self.server.password)
        except OSError:
            pass


def read_ssh_password(path):
    """Read an explicitly supplied private file, never log its contents."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 4097):
                raise InventoryError('SSH password file must be a regular private file owned by the current user (chmod 600)')
            password = stream.read(4098).decode('utf-8')
    except (OSError, UnicodeError):
        raise InventoryError('Cannot read SSH password file; check path, ownership, permissions and UTF-8 encoding') from None
    if password.endswith('\n'):
        password = password[:-1]
    if not password or len(password) > 4096 or any(c in password for c in '\r\n\x00'):
        raise InventoryError('SSH password file must contain one nonempty line')
    return password


@contextmanager
def ssh_session(config, interactive=False, password_file=None):
    if interactive and password_file:
        raise InventoryError('Choose interactive SSH or a password file, not both')
    if not interactive and not password_file:
        yield
        return
    if interactive and not sys.stdin.isatty():
        raise InventoryError("--ssh-interactive requires an interactive terminal")
    password = read_ssh_password(password_file) if password_file else getpass.getpass("OpenNMS SSH password (once for this run): ")
    if not password or len(password) > 4096 or any(c in password for c in "\r\n\x00"):
        raise InventoryError("Empty or unsupported SSH password")
    # A private local socket hands the in-memory password to OpenSSH's askpass
    # helper. No secret in helper files, command-line arguments or env vars.
    with tempfile.TemporaryDirectory(prefix="onms-", dir="/tmp") as directory:
        endpoint = str(Path(directory) / "password.sock")
        helper = Path(directory) / "askpass"
        helper.write_text(ASKPASS_HELPER)
        helper.chmod(0o700)
        server = socketserver.UnixStreamServer(endpoint, PasswordHandler)
        os.chmod(endpoint, 0o600)
        server.password = (password + "\n").encode()
        password = ""
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.1), daemon=True)
        thread.start()
        env = os.environ.copy()
        env.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=env.get("DISPLAY") or ":0", OPENNMS_ASKPASS_SOCKET=endpoint)
        config["_ssh_password_env"] = env
        try:
            yield
        finally:
            config.pop("_ssh_password_env", None)
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            server.password = b""


def walk(config, node, timeout):
    # Strict validation above deliberately excludes Karaf/shell metacharacters.
    command = f'opennms:snmp-walk -l "{node["location"]}" {node["address"]} .{OID}'
    password_env = config.get("_ssh_password_env")
    options = ["-o", "BatchMode=no" if password_env else "BatchMode=yes"]
    if password_env:
        options += ["-o", "NumberOfPasswordPrompts=1", "-o", "PubkeyAuthentication=no",
                    "-o", "PreferredAuthentications=keyboard-interactive,password",
                    "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                ["ssh", "-T", *options, "-o", "ConnectTimeout=10",
                 "-o", "StrictHostKeyChecking=yes", config["ssh_target"], command],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                env=password_env, start_new_session=bool(password_env),
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise SnmpTimeout("Walk deadline exceeded; continuing with next host") from None
        if result.returncode:
            errors.seek(0)
            diagnostic = errors.read(65536).lower()
            if any(marker in diagnostic for marker in (b"permission denied", b"authentication failed", b"no supported authentication methods")):
                raise SshAuthenticationError("OpenNMS SSH authentication failed; stopping to avoid repeated login attempts")
            raise InventoryError("OpenNMS SSH command failed; continuing with next host")
        if output.tell() > LIMIT:
            raise InventoryError("Walk output exceeds size limit")
        output.seek(0)
        raw = output.read()
        capture_dir = config.get("_walk_output_dir")
        capture = None
        if capture_dir:
            capture = Path(capture_dir) / f'{node_file_stem(node)}.txt'
            capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
        if b"SnmpAgentTimeoutException" in raw or b"Timeout retrieving 'snmp:walk'" in raw:
            raise SnmpTimeout("SNMP timeout; continuing with next host")
        try:
            return parse_walk(raw.decode("utf-8", errors="strict"))
        except (InventoryError, UnicodeDecodeError) as exc:
            if capture:
                raise InventoryError(f"Walk output not accepted; raw output saved to {capture}") from None
            if isinstance(exc, UnicodeDecodeError):
                raise InventoryError("Walk output is not valid UTF-8; use --save-walk-output") from None
            raise InventoryError(f"{exc}; use --save-walk-output to inspect the actual format") from None


def snapshot(config, node, software, started_at):
    digest = hashlib.sha256(json.dumps(software, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {
        "schema_version": 2,
        "source": {"system": "opennms", "id": config["source_id"],
                   "node_id": str(node["node_id"]), "location": node["location"],
                   "address": node["address"], "oid": OID},
        "target": node.get("netbox"), "host": {k: node.get(k) for k in ("label", "foreign_source", "foreign_id", "sys_description", "sys_name", "node_created_at")},
        "started_at": started_at, "observed_at": now(),
        "collection_status": "complete_nonempty", "content_sha256": digest,
        "software": software,
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InventoryError("API redirect refused")


class NetBox:
    def __init__(self, config):
        self.config = config
        token = os.environ.get(config.get("token_env", "NETBOX_TOKEN"), "")
        if not token or any(c.isspace() for c in token):
            raise InventoryError("Missing or invalid NetBox token environment variable")
        self.auth = config.get("auth_scheme", "Token") + " " + token
        self.opener = build_opener(NoRedirect(), HTTPSHandler(
            context=ssl.create_default_context(cafile=config.get("ca_file"))))

    def api(self, method, path, body=None):
        url = self.config["url"].rstrip("/") + path
        request = Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                          headers={"Authorization": self.auth, "Accept": "application/json",
                                   "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise InventoryError("NetBox response exceeds size limit")
                return json.loads(raw)
        except (HTTPError, URLError) as exc:
            code = getattr(exc, "code", None)
            raise InventoryError(f"NetBox request failed (HTTP {code or 'network error'})") from None

    def resolve(self, source_id, node_id):
        identity = f"{source_id}:{node_id}"
        field = self.config["identity_field"]
        matches = []
        for section, kind in [("dcim/devices", "devices"), ("virtualization/virtual-machines", "virtual-machines")]:
            data = self.api("GET", f"/api/{section}/?" + urlencode({f"cf_{field}": identity, "limit": 2}))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise InventoryError("Unexpected NetBox lookup response")
            if data.get("count") != len(data["results"]) or data.get("next") or len(data["results"]) > 1:
                raise InventoryError("Ambiguous/incomplete NetBox identity lookup")
            for record in data["results"]:
                if record.get("custom_fields", {}).get(field) != identity:
                    raise InventoryError("NetBox identity filter not honored")
                if type(record.get("id")) is not int or record["id"] < 1:
                    raise InventoryError("Invalid NetBox object identity")
                matches.append({"type": kind, "id": record["id"]})
        if len(matches) > 1:
            raise InventoryError("OpenNMS identity assigned to multiple NetBox objects")
        return matches[0] if matches else None

    def request(self, method, target, body=None):
        section = "dcim/devices" if target["type"] == "devices" else "virtualization/virtual-machines"
        record = self.api(method, f'/api/{section}/{target["id"]}/', body)
        if not isinstance(record, dict) or record.get("id") != target["id"]:
            raise InventoryError("Unexpected NetBox response identity")
        return record

    def sync(self, snap, apply=False):
        field = self.config["field"]
        target = snap["target"]
        record = self.request("GET", target)
        fields = record.get("custom_fields", {})
        identity = f'{snap["source"]["id"]}:{snap["source"]["node_id"]}'
        if fields.get(self.config["identity_field"]) != identity:
            raise InventoryError("NetBox source identity changed; write refused")
        if field not in fields:
            raise InventoryError("Custom field missing; provision a JSON field first")
        current = fields[field]
        if current:
            source = current.get("source", {}) if isinstance(current, dict) else {}
            wanted = snap["source"]
            if (source.get("id"), source.get("node_id")) != (wanted["id"], wanted["node_id"]):
                raise InventoryError("Custom field belongs to another source/node")
        if current == snap:
            return "unchanged"
        if not apply:
            return "would update"
        # Patch only our dedicated field, never replace an entire Device/VM.
        self.request("PATCH", target, {"custom_fields": {field: snap}})
        if self.request("GET", target).get("custom_fields", {}).get(field) != snap:
            raise InventoryError("NetBox write could not be confirmed; rerun to reconcile")
        return "confirmed"


@contextmanager
def collection_session(state, config, interactive, password_file=None):
    with (state / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InventoryError("Another collection is using this state directory") from None
        with History(state / "history.sqlite3") as history, ssh_session(config, interactive, password_file):
            yield history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="OpenNMS software inventory PoC 3.1")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-config", action="store_true", help="Validate configuration without network access or credentials")
    parser.add_argument("--state", default="state")
    parser.add_argument("--timeout", type=int, default=180)
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--ssh-interactive", action="store_true", help="Ask once for the SSH password and reuse it in memory for this run")
    authentication.add_argument("--ssh-password-file", help="Read SSH password from a private file owned by the current user (unattended runs)")
    parser.add_argument("--save-walk-output", action="store_true", help="Save private raw walk output files for format diagnostics")
    parser.add_argument("--sync-netbox", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture", help="Offline synthetic discovery/interface/walk JSON bundle")
    args = parser.parse_args()
    if args.apply or args.sync_netbox:
        parser.error("NetBox export moved to netbox_inventory.py: use its propose/confirm/sync commands")
    config = load_json(args.config)
    validate(config)
    if args.check_config:
        print("Configuration valid (automatic OpenNMS discovery; no network requests made)")
        return 0
    if args.fixture and args.sync_netbox:
        parser.error("Fixtures cannot contact NetBox")
    if args.sync_netbox and "netbox" not in config:
        parser.error("--sync-netbox requires NetBox connection settings")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")
    fixture = load_json(args.fixture) if args.fixture else None
    state = Path(args.state) / config["source_id"]
    state.mkdir(parents=True, exist_ok=True)
    if args.save_walk_output:
        config["_walk_output_dir"] = str((state / "walk-output").resolve())
    with collection_session(state, config, args.ssh_interactive and fixture is None, args.ssh_password_file if fixture is None else None) as history:
        report = {"schema_version": 2, "started_at": now(), "source_id": config["source_id"],
                  "synthetic": fixture is not None, "nodes": [], "enumeration": "failed"}
        try:
            api = OpenNMS(config["opennms"], fixture=fixture)
            discovered = api.nodes()
        except (InventoryError, OSError, ValueError):
            save_json(state / "discovery.status.json", report)
            raise
        report["enumeration"] = "complete"
        failures = 0
        for row in discovered:
            started_at = now()
            node_id = str(row["id"])
            stem = node_file_stem({"node_id": node_id, "label": row.get("label")})
            display = f"node {node_id} ({row.get('label') or 'unnamed'})"
            status = {"attempted_at": started_at, "node_id": node_id,
                      "label": row.get("label"), "location": row.get("location")}
            try:
                node = api.resolve(row)
                if node is None:
                    status.update(collection="skipped", reason="no_primary_snmp_interface")
                    print(f"{display}: no primary SNMP interface")
                else:
                    status.update(address=node["address"], collection="failed")
                    software = parse_walk(fixture["walks"].get(node_id, "")) if fixture is not None else walk(config, node, args.timeout)
                    snap = snapshot(config, node, software, started_at)
                    snap["schema_version"] = 2
                    snap["synthetic"] = fixture is not None
                    history.observation(snap)
                    save_json(state / f"{stem}.json", snap)
                    status.update(collection="complete", observed_at=snap["observed_at"], count=len(software), snapshot_file=f"{stem}.json")
                    print(f'{display}: {len(software)} software entries; recorded in SQLite history')
            except SshAuthenticationError as exc:
                status.update(collection="failed", reason="ssh_authentication_failed", error=str(exc))
                save_json(state / f"{stem}.status.json", status)
                history.attempt(config["source_id"], status)
                report["nodes"].append(status)
                report.update(finished_at=now(), stopped="ssh_authentication_failed", discovered=len(discovered))
                save_json(state / "inventory.json", report)
                raise
            except (InventoryError, OSError, ValueError, KeyError, TypeError) as exc:
                failures += 1
                if isinstance(exc, SnmpTimeout):
                    status["reason"] = "snmp_timeout"
                status.setdefault("collection", "failed")
                status["error"] = str(exc) if isinstance(exc, InventoryError) else type(exc).__name__
                print(f'{display}: {status["error"]}', file=sys.stderr)
            save_json(state / f"{stem}.status.json", status)
            history.attempt(config["source_id"], status)
            report["nodes"].append(status)
        report.update(finished_at=now(), discovered=len(discovered),
                      collected=sum(x["collection"] == "complete" for x in report["nodes"]),
                      skipped=sum(x["collection"] == "skipped" for x in report["nodes"]), errors=failures)
        history.report(report)
        save_json(state / "inventory.json", report)
        save_json(state / "discovery.status.json", {k: v for k, v in report.items() if k != "nodes"})
        print(f'OpenNMS: {report["discovered"]} nodes discovered, {report["collected"]} collected, {report["skipped"]} without SNMP primary, {failures} errors')
        return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (InventoryError, KeyError, TypeError, OSError, ValueError, sqlite3.Error) as exc:
        if isinstance(exc, InventoryError):
            message = str(exc)
        elif isinstance(exc, KeyError):
            message = f"Missing input field: {exc.args[0]} (check configuration/API version)"
        elif isinstance(exc, json.JSONDecodeError):
            message = f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        elif isinstance(exc, sqlite3.Error):
            message = f"History database error: {exc}"
        elif isinstance(exc, OSError):
            message = f"File/system error: {exc.strerror or type(exc).__name__}"
        else:
            message = f"Invalid configuration/input type or value ({type(exc).__name__})"
        print(message, file=sys.stderr)
        sys.exit(2)
