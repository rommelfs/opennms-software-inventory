# Connect to NetBox 4.5.3

The NetBox integration runs alongside collection on the OpenNMS server. Automated
tests use simulated NetBox APIs; validate the integration against your own live
NetBox deployment. Existing inventory files can be imported without further SNMP
queries. No devices or VMs are automatically created or deleted.

For installation, see the [README](../README.md). Keep your existing `config.json`
and `state/`, particularly the same `source_id`.

## 1. Import existing results into history without network access

From your existing working directory, for example `/root`:

```bash
python3 netbox_inventory.py --config config.json import
```

This reads `state/<source_id>/inventory.json` and the software snapshot files.
Retained older snapshots for currently unreachable nodes are also imported.
Files belonging to nodes that have disappeared from OpenNMS are not reintroduced
as current hosts. Repeated imports are allowed and deduplicated. The SQLite
database is `state/<source_id>/history.sqlite3`.

Previously overwritten versions cannot be reconstructed retrospectively. Existing
files form the beginning of the history. From now on, `inventory.py` also records
each successful scan and every node status directly in SQLite. A separate import
after each new run is unnecessary. While a collector is running, the manager
reports a busy lock rather than waiting indefinitely.

## 2. Add the NetBox connection

Add or update only the `netbox` section in your existing `config.json`:

```json
"netbox": {
  "url": "https://YOUR-NETBOX",
  "token_env": "NETBOX_TOKEN",
  "auth_scheme": "Token",
  "identity_field": "opennms_node_key",
  "field": "opennms_software_inventory",
  "stale_after_hours": 24
}
```

The URL is the base address without `/api`. If using a private CA, add `ca_file`
with the path to its public CA certificate. TLS verification stays enabled and
redirects are rejected. Do not switch to HTTP or disable certificate verification.
Use `auth_scheme: Token` for a classic token and `Bearer` for a v2 token (`nbt_...`).
The token itself does not belong in `config.json`.

In a Bash terminal, read the token without adding it to shell history:

```bash
read -rs -p 'NetBox API token: ' NETBOX_TOKEN
export NETBOX_TOKEN
```

Candidate proposals require read access to devices, VMs and IP addresses.
Export also requires change permissions on the selected devices/VMs. The
application limits its updates to its own fields; NetBox object permissions
are not a separate restriction on individual custom fields. Creating the custom
fields requires suitable Extras permissions once. An administrator can provision
the fields and then use a more restricted token for routine operation.

## 3. Check and provision the NetBox fields once

First read the current configuration and display a concrete preview:

```bash
python3 netbox_inventory.py --config config.json setup-fields
```

Then create any missing fields:

```bash
python3 netbox_inventory.py --config config.json setup-fields --apply
```

The following fields are provisioned on both `dcim.device` and
`virtualization.virtualmachine`:

| Field | Type and settings |
|---|---|
| `opennms_node_key` | Text, exact filtering |
| `opennms_software_inventory` | JSON, default `{}` |
| `software_inventory_observed_at` | Date/time |
| `software_inventory_status` | Text |

Status values are `current`, `stale`, `collection_failed` and `source_missing`.
Using text avoids an additional choice-set dependency. Existing fields are
checked for type, object types and exact identity filtering, but are not
reconfigured automatically. No NetBox plugin is needed. Existing device/VM
data is retained.

## 4. Generate mapping proposals using only NetBox GET requests

```bash
python3 netbox_inventory.py --config config.json propose --output mapping.json
```

Proposals use:

- Exact node names/FQDNs matched to device/VM names. Case and a trailing DNS dot
  are normalized; short-name heuristics are not used.
- DNS names (`dns_name`) of assigned NetBox IP addresses.
- SNMP IPs matched against IP addresses assigned to device and VM interfaces.

Ambiguous names or private IPs can produce several candidates. The plan lists
candidates, `matched_name`, `matched_ip`, `matched_dns`, site and tenant. No
candidate is approved automatically: `approve` is always `false`. For a single
candidate, `target` is already populated as a suggestion. For multiple candidates,
choose the correct entry from `candidates` and set the corresponding `type`/`id`
in `target`. Set `approve: true` only for entries you have reviewed; leave the
others unchanged. Alternatively, use the [web interface](web-ui.md) to review
and confirm mappings without editing JSON.

For overlapping networks and multiple sites, `netbox.location_scope` can limit
OpenNMS locations to NetBox sites, tenants or VRFs, for example:

```json
"location_scope": {
  "Default": {"site_id": 1, "tenant_id": 2, "vrf_id": null},
  "Branch Berlin": {"site_id": 7, "tenant_id": 2, "vrf_id": 9}
}
```

Specify only the restrictions you actually want. Without a scope, a review
warning is displayed. OpenNMS locations are not automatically equated to NetBox
sites. A matching hostname alone does not prove machine identity. NetBox matching
does not replace OpenNMS discovery or restrict its host list.

## 5. Confirm reviewed mappings locally

Preview and revalidate the edited plan against live data:

```bash
python3 netbox_inventory.py --config config.json confirm --plan mapping.json
```

Save confirmed bindings in SQLite:

```bash
python3 netbox_inventory.py --config config.json confirm --plan mapping.json --apply
```

NetBox is read again, but remains unchanged. Changed host/candidate evidence is
rejected. Two nodes cannot bind to the same NetBox object. Existing bindings are
not automatically moved to a different target. Bindings record the source, node
ID, fixed NetBox base URL, device/VM type, object ID, confirmation timestamp and
evidence. Subsequent exports use that binding rather than matching hostnames
again. Changing the NetBox endpoint does not silently transfer the binding.

This version does not provide an automatic rebind command for correcting an
existing binding. Such cases need explicit review; do not work around this by
renaming `source_id` or editing the database manually.

## 6. Export software inventory

Create a preview using only NetBox reads:

```bash
python3 netbox_inventory.py --config config.json sync
```

Then apply changes:

```bash
python3 netbox_inventory.py --config config.json sync --apply
```

The first export sets the source identity on the confirmed target and writes
the software inventory. Foreign source identities or software fields, duplicate
identities and a newer observation already in NetBox cause a visible error.
Each PATCH changes only the four dedicated custom fields and adds a changelog
message. A GET verifies the stored state. If a response is uncertain, rerun
`sync`: each write is preceded by a read. Repeating the same observation is a no-op.

If software is unchanged in a new scan, the large package list is not sent again;
only changed metadata, such as the observation timestamp, may require a PATCH.
NetBox may still store the entire object state in its changelog. Choose retention
settings appropriate for the data volume.

Invalid or missing bindings are reported per host, and processing continues.
`state/<source_id>/netbox-sync.json` contains the latest export report; SQLite
also records export attempts. Synthetic fixture data is never exported. Assets
are not created or deleted. Scheduled runs use `sync --confirmed-only` to report
unbound nodes as `skipped` without treating them as export failures; errors on
bound hosts remain visible.

## 7. Subsequent scans and history

```bash
python3 inventory.py --config config.json --ssh-interactive
python3 netbox_inventory.py --config config.json sync
python3 netbox_inventory.py --config config.json sync --apply
```

An SNMP timeout does not stop the remaining hosts. The collector can return exit
code 1 even when many hosts were collected successfully. A subsequent export can
still transfer those successful observations, so do not blindly chain it to the
collector using `&&`. For new nodes, generate a new plan and approve their mappings;
no intermediate import is needed. Collection records them even without a NetBox
mapping. For unattended operation, use the [automation guide](automation.md).

The old collector flags `--sync-netbox`/`--apply` have been blocked since version
3.0. Export runs through the manager using confirmed bindings.

For example, view version transitions for node 3:

```bash
python3 netbox_inventory.py --config config.json history --node 3
```

History uses `baseline`, `newly_observed`, `version_changed` and
`no_longer_observed`. The last value is not a confirmed uninstallation.
Observation timestamps are shown, rather than inferred installation timestamps.
Identical inventories share their stored package list, while individual
observations and failures are retained. Transitions such as A → B → A remain
visible in the timeline. Hostname changes do not change historical identity;
the source and node ID are authoritative.

Timeouts do not overwrite a successful software observation. NetBox retains
that package list and observation timestamp, with status `collection_failed`
when the current scan fails. Without a new scan, the inventory becomes `stale`
after `stale_after_hours`. Bound nodes that disappear from OpenNMS are marked
`source_missing`; their NetBox assets and last package inventories are retained.
A job that never starts cannot report its own failure, so execution needs
separate monitoring.

## 8. Backups and operational limits

Create a consistent SQLite backup with the collector/manager stopped:

```bash
python3 netbox_inventory.py --config config.json backup --output history-backup.sqlite3
```

The destination must not exist. The backup contains both history and confirmed
bindings. Also back up `config.json` and the JSON results; manage tokens separately.
Keep SQLite and state directories on persistent local storage. Do not publish
the database or operational files in Git. The application provides local locks
only: do not run parallel writers with separate state directories against the
same NetBox inventory fields. There is no global NetBox transaction or API CAS.
Only one process should manage these four custom fields.

If a bound source changes its known `foreignSource`, `foreignId` or node creation
time, its export stops for review. Older snapshots do not include node creation
time. Replacing a physical host or VM while retaining its OpenNMS identity cannot
be detected reliably without additional identity data. Do not treat `source_id`
and node IDs as freely reusable names. Reinstalling OpenNMS is an explicit source
change.

Scheduling templates are available but must be installed and enabled explicitly.
`--ssh-interactive` asks for a password once per run and cannot be used for
unattended scheduling. `--ssh-password-file` supports unattended password access;
see the [systemd setup](automation.md).

The custom-field REST contracts were checked against the
[NetBox v4.5.3 source](https://github.com/netbox-community/netbox/blob/v4.5.3/netbox/extras/api/serializers_/customfields.py)
and the [REST API documentation](https://netboxlabs.com/docs/netbox/v4.5/integrations/rest-api/).

Not included: automatic asset creation, automatic rebindings, a NetBox package
model/plugin or Bearings integration. SQLite history and explicit source bindings
provide a foundation for those extensions. The existing [web interface](web-ui.md)
provides mapping review and export without manually editing plans.
