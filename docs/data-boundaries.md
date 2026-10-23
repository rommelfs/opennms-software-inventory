# Operational data and source code

The repository contains only application code, documentation, tests and synthetic
examples.

Keep the following outside the checkout:

- Your actual `config.json`, API tokens, SSH keys and private CA keys.
- Collected host/software files and raw SNMP output.
- SQLite history and backups.
- Review plans such as `mapping.json`.
- Scheduled-run credentials, including `credentials.env` and `karaf-password`.

The `.gitignore` adds protection for common local filenames. It does not replace
checking files with different names or files copied from other directories.

Recommended migration on ccq: clone to `/opt/opennms-software-inventory`, keep
existing data under `/root/state`, and keep the configuration at `/root/config.json`.
Use absolute `--config` and `--state` paths in every command. This avoids
accidentally creating a new database inside the checkout.

Use the CLI `backup` command for consistent database backups. Store the resulting
SQLite backup together with the configuration and required operational files
in a separate backup location. Manage tokens separately and protect them.

NetBox stores the current software inventory. SQLite holds the complete local
observation history and confirmed bindings.
