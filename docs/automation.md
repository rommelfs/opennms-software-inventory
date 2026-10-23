# Scheduled operation on ccq

The supplied systemd service and timer collect inventory through OpenNMS daily,
then export previously confirmed mappings to NetBox. They continue to use
`/root/config.json` and `/root/state`. New nodes are discovered automatically
through OpenNMS and recorded in local history; confirm their NetBox mappings
in the web interface. The interface does not need to be running for scheduled
collection and export.

## Authentication with OpenNMS 35.0.5

SSH public-key authentication for Karaf on the Core is documented as a new feature
in Horizon 36.0.2 ([release notes, NMS-19953](https://github.com/OpenNMS/opennms/releases/tag/opennms-36.0.2-1)).
For 35.0.5, the template therefore uses a password from an explicitly specified
file. This does not require an OpenNMS upgrade.

The password is stored persistently in plaintext in a file readable only by root.
The collector rejects group/world-readable files, incorrect owners, symlinks,
empty files and multiline passwords. Keep the file and its directory outside
Git. The SSH password is not placed in SSH command-line arguments, environment
variables or journal logs; the existing private ASKPASS channel passes it to
OpenSSH. Interactive authentication remains available with `--ssh-interactive`.

## One-time preparation

On ccq, as root:

```bash
cd /opt/opennms-software-inventory
git pull --ff-only
install -d -m 700 /etc/opennms-software-inventory
(umask 077; touch /etc/opennms-software-inventory/credentials.env /etc/opennms-software-inventory/karaf-password)
chmod 600 /etc/opennms-software-inventory/credentials.env /etc/opennms-software-inventory/karaf-password
nano /etc/opennms-software-inventory/credentials.env
```

Put the API credentials in `credentials.env`. Variable names must match the
`username_env`, `password_env` and `token_env` settings in your `config.json`.
The default names are:

```ini
OPENNMS_USERNAME='YOUR_API_USERNAME'
OPENNMS_PASSWORD='YOUR_API_PASSWORD'
NETBOX_TOKEN='YOUR_NETBOX_TOKEN'
```

Use assignments only, without `export`, shell commands or variable references.
systemd reads this file as an `EnvironmentFile`, not as a shell script. The values
above are placeholders. For passwords containing quotation marks, follow the
[EnvironmentFile syntax](https://www.freedesktop.org/software/systemd/man/251/systemd.exec.html#EnvironmentFile=).

Next:

```bash
nano /etc/opennms-software-inventory/karaf-password
```

Enter only the SSH password for `admin@localhost:8101` on a single line, without
a variable name. A final newline is allowed. The SSH target remains the
`ssh_target` from `config.json`. With an SSH alias, the service runs as root and
uses the same `/root/.ssh/config` and known host keys as your previous manual
runs. The host key must already be trusted; strict verification remains enabled.

The NetBox custom fields must already exist, and the token must have the required
read and change permissions. The daily job does not create fields or approve
new mappings.

## Run a complete trial first

This trial collects real software inventory and records its history. The NetBox
export remains a preview because `--apply` is omitted. API credentials are loaded
from the environment file in the same way as in the scheduled service:

```bash
systemd-run --unit=opennms-inventory-check --wait --pipe --collect \
  --property=EnvironmentFile=/etc/opennms-software-inventory/credentials.env \
  --property=UMask=0077 \
  /usr/bin/python3 /opt/opennms-software-inventory/inventory_cycle.py \
  --config /root/config.json --state /root/state \
  --ssh-password-file /etc/opennms-software-inventory/karaf-password
```

Collection continues when individual hosts are unreachable. In that case the
cycle returns exit code 1, but still creates the export preview. An incorrect SSH
password, failed discovery or incomplete overall run prevents export. If there
are errors, inspect the log and `cycle.status.json` before proceeding.

## Install the service and timer

The service includes `--apply`: confirmed targets will actually be updated.
Cloning the repository does not activate the units:

```bash
cd /opt/opennms-software-inventory
install -m 644 deploy/systemd/opennms-software-inventory.service /etc/systemd/system/
install -m 644 deploy/systemd/opennms-software-inventory.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start opennms-software-inventory.service
journalctl -u opennms-software-inventory.service -n 100 --no-pager
systemctl enable --now opennms-software-inventory.timer
systemctl list-timers opennms-software-inventory.timer
```

Default schedule: daily from 02:00, with up to ten minutes of random delay, in
the server time zone. `Persistent=true` catches up on a missed scheduled run
when the timer is activated after being stopped or powered off. The timer does
not start a second instance of an already running service. See
[systemd.timer](https://www.freedesktop.org/software/systemd/man/251/systemd.timer.html).

## Change the frequency

For example, run every six hours starting at midnight:

```bash
systemctl edit opennms-software-inventory.timer
```

Set this drop-in content:

```ini
[Timer]
OnCalendar=
OnCalendar=*-*-* 00,06,12,18:00:00
```

Then:

```bash
systemctl daemon-reload
systemctl restart opennms-software-inventory.timer
systemctl list-timers opennms-software-inventory.timer
```

The empty `OnCalendar=` clears the previous daily schedule before adding the
new one. Start with daily runs and increase the frequency only after measuring
run times: SNMP walks run sequentially, and timeouts can extend collection.
The timer does not overlap an active service. The `.cycle.lock` also prevents
multiple manual cycle invocations for the same source.

## Operation and errors

```bash
systemctl status opennms-software-inventory.service
journalctl -u opennms-software-inventory.service --since today
systemctl list-timers opennms-software-inventory.timer
```

The directory `/root/state/<source_id>/` also contains:

- `cycle.status.json`: status of the latest cycle, its steps and exit codes.
- `inventory.json`: latest collection run, including host failures.
- `netbox-sync.json`: latest export or export preview.
- `history.sqlite3`: complete local observation and export history.

Cycle exit codes:

| Code | Meaning |
|---|---|
| 0 | Collection and export/preview completed |
| 1 | Individual host queries failed; overall collection and export/preview still completed |
| 2 | Preparation, overall collection or export failed; see the status file and log |

Exit code 1 is deliberately shown as a failure by systemd. Successful hosts
were still processed by the export step. Missing mappings are not export errors:
`sync --confirmed-only` reports them as `skipped`, without creating assets or
guessing mappings. Errors on already bound hosts remain visible.

The existing protections still apply: foreign identities are not taken over,
and newer NetBox observations are not overwritten. A failed host scan preserves
its last successful software inventory; the export may mark it as stale or as a
collection failure. An aborted or old overall report is not exported as a new run.

While the collector or exporter holds the data lock, mutating UI operations
reject concurrent matching attempts. Try again afterwards. After a new scan,
select **Match NetBox** if you want to map newly discovered nodes. Back up the
SQLite database regularly using the existing `backup` command.

For code updates, temporarily stop the timer and let any running service finish
first. Then run `git pull --ff-only`. Updated unit templates take effect only
after you copy them again and run `daemon-reload`; custom systemd drop-ins are
retained. To disable scheduling:

```bash
systemctl disable --now opennms-software-inventory.timer
```

This stops future triggers. An already running collection remains active and
can finish normally.
