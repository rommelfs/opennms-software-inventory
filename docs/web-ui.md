# Inventory Web 3.1: review mappings in your browser

The web interface runs alongside the existing tools on the OpenNMS server.
It uses the same `config.json` and SQLite history. It needs no additional Python
packages, frontend build or external JavaScript/font services.

## 1. Install with Git

See the [README](../README.md) for installation and updates. Clone the complete
checkout: the `web` directory is part of the application. Keep configuration
and inventory data outside the checkout.

## 2. Start the interface

Provide the NetBox token through `NETBOX_TOKEN`, as for the CLI, or through the
token environment variable named in `config.json`. The token stays on the server.
The web interface itself does not need OpenNMS API or SSH credentials; it works
with already collected inventory data.

```bash
python3 inventory_web.py --config config.json --state ./state
```

On startup, existing results are imported if the local lock is available. If a
collector is running or an import is deferred, the interface remains available
and displays a notice. Use **Import results** once collection finishes. Current
collector versions write directly to SQLite; importing again is safe and deduplicated.

The console prints a link in this format:

```text
http://127.0.0.1:8765/#token=<session-token>
```

The server binds only to `127.0.0.1`, not to a public address. It runs in the
foreground; press `Ctrl+C` to stop it. A restart creates a new access link without
losing inventory data or bindings.

## 3. Open an SSH tunnel from your workstation

In a second terminal on **your workstation**, not on ccq:

```bash
ssh -N -L 8765:127.0.0.1:8765 root@ccq
```

Replace `root@ccq` with your usual SSH account/alias for the OpenNMS server if
needed. This is the normal server SSH connection (typically port 22), not the
Karaf connection on port 8101.

Open the complete console link, including `#token=...`, in your workstation
browser. If port 8765 is already in use, start the interface with `--port 8766`
and use port 8766 on both sides of the tunnel. Host/Origin validation expects
the same port in the browser and on the server.

The session token authorizes access to the local workspace. It is not sent as
a URL query: the browser reads it from the fragment and uses it for API requests.
Every data API request requires the token. NetBox tokens, API passwords and SSH
passwords are not sent to the browser. Access is intended for an operator through
an SSH tunnel, rather than a public multiuser service.

## 4. Map nodes without editing JSON

- **Match NetBox** reads all devices, VMs and assigned IP addresses from NetBox
  and generates candidates. This step does not write to NetBox.
- Use search and filters to find nodes, IPs, NetBox candidates or DNS names and
  display unmapped, confirmed, clear or ambiguous cases.
- Choose one target per node from the dropdown. Each candidate shows its name,
  device/VM type, object ID, site, tenant, IP addresses/VRFs and DNS names.
- The **Name**, **DNS** and **IP** chips show the matching evidence. Targets
  already bound elsewhere are marked as occupied and cannot be selected.
- Select the desired rows using their checkboxes.
- **Confirm selection** displays the number and effect of the selected mappings.
  After confirmation, targets are checked against live NetBox data again and
  bindings are saved locally. NetBox is unchanged by this step.

Name matching compares the node label and, when available in newer collections,
`sysName` with NetBox object names and the `dns_name` of assigned NetBox IP
addresses. Case and a trailing DNS dot are normalized. No additional external
DNS queries are made. Arbitrary short names are not heuristically equated to FQDNs.

Multiple candidates require an explicit choice. Scope rules from
`netbox.location_scope` still apply; see the [NetBox guide](netbox-setup.md).
A single candidate is not automatically approved. If candidates are missing,
check NetBox names, DNS names and IP assignments. This version does not create
assets or allow an unchecked manual binding to arbitrary object IDs.

## 5. Preselect clear matches in bulk

**Select clear matches** selects currently visible rows only when:

- There is exactly one candidate.
- The name or DNS name **and** the SNMP IP match.
- No other node claims the same candidate.
- The target has no conflicting source identity or local binding.
- Private/non-global IPs have a nonempty configured location scope.
- The data is real and the node has no existing binding.

Review the preselected rows, then confirm them together. The preselection button
does not save bindings. Other cases can be selected individually. Missing scope
rules explain why a matching private IP may not qualify for bulk selection.

Existing mappings are displayed and protected against automatic reassignment.
Renaming a host preserves the same node ID/NetBox object ID association.
Bindings are never silently reassigned.

## 6. View software and history

Click a node name or its detail arrow to open a dialog containing:

- A searchable package list with versions and architectures.
- Version history with observation timestamps.
- Newly observed and no-longer-observed packages.

**Software & history** also lists all current nodes with package counts, their
last successful observation timestamp and collection status. A timeout does not
remove an earlier successful software inventory.

## 7. Export to NetBox from the interface

Provision the four custom fields once, as described in the NetBox guide. If
needed, run these commands in a terminal:

```bash
python3 netbox_inventory.py --config config.json setup-fields
python3 netbox_inventory.py --config config.json setup-fields --apply
```

Then open **NetBox export** in the browser:

- **Create export preview** reads confirmed targets and shows which inventory
  fields would change for each node. It does not write anything.
- **Write changes to NetBox** becomes available after a suitable preview.
  The subsequent confirmation starts the actual PATCH requests.
- If local observations or bindings change after the preview, the export is
  rejected. NetBox results are also checked again before writing. Create a new
  preview if either side has changed.
- Synchronizing the same observation again is a no-op. If a response is lost,
  the next attempt reads the stored state again rather than creating additional
  assets or duplicate history entries.

No device or VM is created or deleted. Export and local mapping remain separate
operations. CLI locks also apply to web operations: a collector or another
manager cannot overwrite the same local data concurrently. The web interface
runs only one operation at a time. This is not a global NetBox transaction or
an atomic compare-and-swap API.

## 8. Demo and technical validation

Run without configuration, NetBox or external network access:

```bash
python3 inventory_web.py --demo
```

The demo includes eight synthetic nodes, DNS matches, an ambiguous case, a
timeout, an existing mapping and version transitions. It uses a temporary
directory; saving bindings and exporting to NetBox are disabled. The printed
session token and, where required, an SSH tunnel are still needed.

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover real SQLite files, HTTP handlers including authentication and Origin
checks, DNS matching, selection logic, live revalidation and export previews.
HTTP handlers can be tested without opening a network socket. SSH password-helper
tests are skipped in sandboxes that prohibit Unix socket bindings.

Validation scope: JavaScript syntax and backend behavior are checked automatically.
A graphical browser could not be launched in the original development environment,
so full visual browser acceptance is not part of that verification. Validate
your live NetBox 4.5.3 deployment separately from the simulated API tests.

## Conflicts when confirming mappings

Each NetBox device or VM can be mapped to only one OpenNMS node. If several
selected nodes use the same target, the interface highlights those rows and
disables **Confirm selection**. Use the **Conflicts** filter to display them
together, including rows hidden by a previous filter; clear any search text if
needed. Compare node IDs, IP addresses and locations to determine which node
belongs to the asset, then deselect the other checkbox or choose another matching
target.

All selected candidates are read from NetBox again before saving. A target that
has been removed, assigned elsewhere or changed is reported with its hostname,
node ID and target ID. Select **Match NetBox** again and review the affected
selection. A new matching operation resets the current selection. A rejected
confirmation preserves the selection, saves no mappings from that request and
changes no NetBox fields. Previously confirmed bindings are retained.
