# OpenNMS Software Inventory

Software inventory for Linux servers through **OpenNMS and Minions**, with local
version history and a web interface for mapping nodes to existing NetBox assets.

OpenNMS supplies the complete host list, primary SNMP addresses and locations.
The application queries `hrSWInstalledName` through OpenNMS, stores observations
in SQLite and exports the latest software inventory to dedicated NetBox custom fields.

**Status: working proof of concept, version 3.1.** The collector has been used on
the OpenNMS server. Automated tests use simulated NetBox APIs; full deployment
acceptance against NetBox 4.5.3 and visual browser acceptance are separate checks.
The application does not create or delete assets.

## Install on the OpenNMS server

Requirements: Linux, Git, Python 3.8+ with `sqlite3`, and OpenSSH.
No additional Python packages, Node.js installation or containers are required.

```bash
git clone https://github.com/rommelfs/opennms-software-inventory.git /opt/opennms-software-inventory
cd /opt/opennms-software-inventory
python3 -m py_compile inventory.py inventory_cycle.py inventory_store.py netbox_inventory.py inventory_web.py inventory_web_demo.py
python3 inventory.py --version
```

The public repository can be cloned over HTTPS without GitHub authentication.
If you prefer SSH and have configured a suitable key, use
`git@github.com:rommelfs/opennms-software-inventory.git`.
Do not put a token in the Git URL.

### Migrate from the existing files on ccq

**Keep `/root/config.json` and `/root/state` where they are.**
Do not change `source_id`. Migration does not require another full SNMP scan.

```bash
cd /opt/opennms-software-inventory
python3 netbox_inventory.py --config /root/config.json --state /root/state import
python3 inventory_web.py --config /root/config.json --state /root/state
```

`--state` specifies the parent directory. With `source_id: opennms`, the database
is therefore `/root/state/opennms/history.sqlite3`.

Open the SSH tunnel on your workstation:

```bash
ssh -N -L 8765:127.0.0.1:8765 root@ccq
```

Then open the link printed at web server startup, including `#token=...`, in your
browser. The web server listens only on `127.0.0.1:8765`. It runs in the foreground;
press `Ctrl+C` to stop it. You can keep the previous standalone Python files for
now; run the application from this checkout going forward.

### New installation

Choose a template and save your configuration **outside the checkout**:

- [OpenNMS without NetBox](examples/config.opennms-only.json)
- [OpenNMS and NetBox](examples/config.json)

API credentials are read from the environment variables named in the configuration.
The example data is synthetic. Update the URLs for your deployment.
`ssh_target` is an SSH configuration alias for the OpenNMS Karaf shell, for example:

```sshconfig
Host opennms-karaf
    HostName localhost
    Port 8101
    User admin
```

SSH connects to the Karaf shell, rather than to each target host.

## Update on ccq

Stop the web interface with `Ctrl+C` and let any active collector finish:

```bash
cd /opt/opennms-software-inventory
git pull --ff-only
python3 inventory_web.py --config /root/config.json --state /root/state
```

Configuration, SQLite history and confirmed mappings remain outside the checkout;
`git pull` does not replace them. Back up the database along with the other
operational data. To create a consistent backup:

```bash
python3 netbox_inventory.py --config /root/config.json --state /root/state backup --output /root/history-backup.sqlite3
```

The backup destination must not exist. Do not let multiple application versions
write to the same operational data at the same time. For scheduled deployments,
also follow the update instructions in the [automation guide](docs/automation.md).

## Typical workflow

1. Import previously collected files. Subsequent scans record their history automatically.
2. In the web interface, select **Match NetBox** to compare candidates by object name,
   `dns_name` and assigned IP address.
3. Select and confirm targets. Clear matches can be preselected in bulk. This step
   saves local bindings and leaves NetBox unchanged.
4. Under **NetBox export**, create a preview and explicitly apply the changes.
   Changes since the preview require a new preview.

Provision the custom fields once before the first export:

```bash
python3 netbox_inventory.py --config /root/config.json --state /root/state setup-fields
python3 netbox_inventory.py --config /root/config.json --state /root/state setup-fields --apply
```

A suitable NetBox token must be available in the configured environment variable.
It is never sent to the browser.

Run subsequent scans with:

```bash
python3 inventory.py --config /root/config.json --state /root/state --ssh-interactive
```

The interactive SSH password is requested once per run and kept only in memory.
SNMP timeouts are recorded and processing continues with the remaining hosts.
Unattended runs need SSH access without an interactive prompt.
`--ssh-password-file` supports scheduled password authentication on older OpenNMS
versions. Keep that file outside the repository, owned by the executing user,
and inaccessible to other users (recommended permissions: `chmod 600`).

## Scheduled collection and export

The [systemd guide](docs/automation.md) describes deployment on ccq with OpenNMS
35.0.5. The unit templates under `deploy/systemd/` schedule a daily run between
02:00 and approximately 02:10 in the server time zone. They become active only
after you install them on the server and enable the timer.

`inventory_cycle.py` collects all OpenNMS nodes, then synchronizes only existing,
confirmed bindings. Without `--apply`, the export is a preview. New nodes are
not exported until you confirm their mappings in the web interface. Individual
host failures do not prevent successful results from being exported. An aborted
overall collection does not start an export.

## Documentation

- [Web interface, SSH tunnel, candidate selection and export](docs/web-ui.md)
- [NetBox fields, mapping model, history and CLI](docs/netbox-setup.md)
- [Operational data and repository contents](docs/data-boundaries.md)
- [Automation, password file and systemd timer](docs/automation.md)

Collection reports **installed packages**. It is not a complete container SBOM,
proof of running processes or an automatic CVE applicability assessment.
Version transitions have observation timestamps; actual installation times are
not inferred.

## Demo

```bash
python3 inventory_web.py --demo
```

The demo uses eight synthetic nodes in a temporary directory. It needs no NetBox
instance and performs no external queries or exports.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The JavaScript candidate-selection checks also require Node.js for development
(it is not required to run the application on the server):

```bash
node --check web/app.js
node --test tests/test_mapping.cjs
```

Tests use temporary SQLite databases and simulated NetBox APIs. Local SSH
password-helper tests are skipped if the environment prohibits Unix socket
bindings. HTTP handlers are also tested without opening a network socket.

## Project structure

| File | Purpose |
|---|---|
| `inventory.py` | OpenNMS discovery and SNMP collection |
| `inventory_cycle.py` | Scheduled collection and export of confirmed mappings |
| `inventory_store.py` | SQLite history and persistent bindings |
| `netbox_inventory.py` | CLI import, candidate proposals, approval and export |
| `inventory_web.py` | Local web interface and operator API |
| `inventory_web_demo.py` | Isolated demo data |
| `web/` | HTML, CSS and JavaScript without a build step |
| `examples/` | Synthetic configurations and fixtures only |
