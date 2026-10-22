# Betriebsdaten und Quellcode

Das Repository enthält ausschließlich Programmcode, Dokumentation, Tests und
synthetische Beispiele.

Außerhalb des Checkouts bleiben:

- Tatsächliche `config.json`, API-Tokens, SSH-Schlüssel und private CA-Schlüssel.
- Erhobene Host-/Softwaredateien und rohe SNMP-Ausgaben.
- SQLite-Historie und Backups.
- Review-Pläne wie `mapping.json`.

Die `.gitignore` schützt die üblichen lokalen Dateinamen zusätzlich. Sie ersetzt
keine Prüfung, wenn Dateien anders benannt oder aus anderen Verzeichnissen
hineinkopiert werden.

Empfohlener Umstieg auf ccq: Checkout unter `/opt/opennms-software-inventory`,
bestehende Daten weiterhin unter `/root/state` und Konfiguration
`/root/config.json`. In allen Aufrufen absolute `--config`- und `--state`-Pfade
verwenden. Dadurch wird nicht versehentlich eine neue Datenbank im Checkout
angelegt.

Für Backups die CLI-Funktion `backup` verwenden und das erzeugte SQLite-Backup
zusammen mit Konfiguration und den erforderlichen Betriebsdateien getrennt
sichern. Tokens separat und geschützt verwalten.

NetBox ist der Speicher für den aktuellen Softwarestand; die vollständige
lokale Beobachtungshistorie und die bestätigten Bindings liegen in SQLite.
