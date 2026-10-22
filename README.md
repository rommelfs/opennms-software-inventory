# OpenNMS Software Inventory

Softwareinventar für Linux-Server über **OpenNMS und Minions**, mit lokaler
Versionshistorie und einer Weboberfläche zur Zuordnung vorhandener NetBox-Assets.

OpenNMS liefert die vollständige Hostliste, primäre SNMP-Adressen und Locations.
Die Anwendung fragt `hrSWInstalledName` über OpenNMS ab, speichert Beobachtungen
in SQLite und überträgt aktuelle Softwarestände in eigene NetBox-Custom-Fields.

**Stand: funktionierender PoC, Version 3.1.** Die Erfassung wurde auf dem
OpenNMS-Server eingesetzt. Der NetBox-Adapter ist mit simulierten APIs getestet;
eine vollständige Live-Abnahme gegen NetBox 4.5.3 und eine grafische
Browserabnahme stehen noch aus. Die Anwendung erstellt oder löscht keine Assets.

## Auf dem OpenNMS-Server installieren

Voraussetzungen: Linux, Git, Python 3.8+ mit `sqlite3`, OpenSSH.
Keine zusätzlichen Python-Pakete, Node.js-Installation oder Container nötig.

```bash
git clone https://github.com/rommelfs/opennms-software-inventory.git /opt/opennms-software-inventory
cd /opt/opennms-software-inventory
python3 -m py_compile inventory.py inventory_store.py netbox_inventory.py inventory_web.py inventory_web_demo.py
python3 inventory.py --version
```

Ein privates Repository benötigt beim Klonen einen geeigneten GitHub-Zugang,
etwa einen auf dieses Repository beschränkten SSH-Deploy-Key. Dann lautet die URL
`git@github.com:rommelfs/opennms-software-inventory.git`.
Kein Token in die Git-URL schreiben.

### Von den bisherigen Dateien auf ccq umsteigen

**`/root/config.json` und `/root/state` bleiben an ihrem Platz.**
`source_id` nicht ändern. Ein erneuter SNMP-Gesamtlauf ist für den Umstieg
nicht erforderlich.

```bash
cd /opt/opennms-software-inventory
python3 netbox_inventory.py --config /root/config.json --state /root/state import
python3 inventory_web.py --config /root/config.json --state /root/state
```

`--state` bezeichnet das übergeordnete Verzeichnis. Bei `source_id: opennms`
liegt die Datenbank also in `/root/state/opennms/history.sqlite3`.

Auf deinem Rechner den SSH-Tunnel öffnen:

```bash
ssh -N -L 8765:127.0.0.1:8765 root@ccq
```

Danach den beim Webserverstart ausgegebenen Link einschließlich `#token=...`
im Browser öffnen. Der Webserver lauscht ausschließlich auf `127.0.0.1:8765`.
Er läuft im Vordergrund; `Ctrl+C` beendet ihn. Die bisherigen einzelnen
Python-Dateien können zunächst liegenbleiben; künftig die Programme aus diesem
Checkout aufrufen.

### Neue Installation

Eine Vorlage auswählen und **außerhalb des Checkouts** als Konfiguration speichern:

- [OpenNMS ohne NetBox](examples/config.opennms-only.json)
- [OpenNMS und NetBox](examples/config.json)

API-Zugangsdaten kommen aus den in der Konfiguration benannten Umgebungsvariablen.
Beispieldaten sind synthetisch. Für die Nutzung die URLs anpassen.
`ssh_target` ist ein SSH-Konfigurationsalias zur OpenNMS-Karaf-Shell, beispielsweise:

```sshconfig
Host opennms-karaf
    HostName localhost
    Port 8101
    User admin
```

Der Zugriff ist zur Karaf-Shell, nicht per SSH zu jedem Zielhost.

## Updates auf ccq

Oberfläche mit `Ctrl+C` stoppen und einen laufenden Collector beenden lassen:

```bash
cd /opt/opennms-software-inventory
git pull --ff-only
python3 inventory_web.py --config /root/config.json --state /root/state
```

Konfiguration, SQLite-Historie und bestätigte Zuordnungen liegen außerhalb
des Checkouts und werden durch `git pull` nicht ersetzt. Die Datenbank wie
die übrigen Betriebsdaten sichern. Ein konsistentes Backup erzeugt:

```bash
python3 netbox_inventory.py --config /root/config.json --state /root/state backup --output /root/history-backup.sqlite3
```

Der Backup-Zielname muss neu sein. Mehrere Programmversionen nicht gleichzeitig
auf dieselben Betriebsdaten schreiben lassen.

## Typischer Ablauf

1. Bereits erhobene Dateien importieren; spätere Scans protokollieren ihre
   Historie automatisch.
2. In der Weboberfläche **NetBox abgleichen**: Kandidaten anhand von Objektname,
   `dns_name` und zugewiesener IP vergleichen.
3. Ziele auswählen und bestätigen. Eindeutige Treffer lassen sich gesammelt
   vorauswählen. Dieser Schritt speichert lokale Bindings und verändert NetBox nicht.
4. Unter **NetBox-Export** eine Vorschau erstellen und anschließend die Änderungen
   bewusst übertragen. Änderungen seit der Vorschau erfordern eine neue Vorschau.

Vor dem ersten Export einmalig die Custom Fields bereitstellen:

```bash
python3 netbox_inventory.py --config /root/config.json --state /root/state setup-fields
python3 netbox_inventory.py --config /root/config.json --state /root/state setup-fields --apply
```

Ein passender NetBox-Token muss dafür in der konfigurierten Umgebungsvariable
bereitstehen. Der Browser erhält ihn nicht.

Weitere Scans:

```bash
python3 inventory.py --config /root/config.json --state /root/state --ssh-interactive
```

Das SSH-Passwort wird einmal pro Lauf abgefragt und nur im Arbeitsspeicher
gehalten. Ein SNMP-Timeout wird protokolliert; die anderen Hosts laufen weiter.
Unbeaufsichtigte Läufe benötigen einen geeigneten SSH-Zugang ohne interaktive
Passwortabfrage. Mit `--ssh-password-file` ist auch bei älteren OpenNMS-Versionen
ein geplanter Passwortzugang möglich. Die Datei bleibt außerhalb des Repositorys
und muss ausschließlich dem ausführenden Benutzer gehören und für ihn lesbar sein
(empfohlen `chmod 600`).

## Periodische Erfassung und Export

Der [systemd-Leitfaden](docs/automation.md) beschreibt die Einrichtung auf ccq
mit OpenNMS 35.0.5. Vorbereitete Units unter `deploy/systemd/` starten täglich
zwischen 02:00 und etwa 02:10 Uhr (Serverzeitzone). Sie werden erst aktiv, wenn
du sie auf dem Server installierst und den Timer einschaltest.

`inventory_cycle.py` erfasst alle OpenNMS-Nodes und synchronisiert anschließend
ausschließlich bestehende bestätigte Bindings. Ohne `--apply` erstellt der
Export eine Vorschau. Neue Nodes bleiben bis zur Zuordnung in der Weboberfläche
ohne Export. Einzelne Hostfehler verhindern den Export erfolgreicher Ergebnisse
nicht. Ein abgebrochener Gesamtlauf startet keinen Export.

## Dokumentation

- [Weboberfläche, SSH-Tunnel, Kandidatenauswahl und Export](docs/web-ui.md)
- [NetBox-Felder, Zuordnungsmodell, Historie und CLI](docs/netbox-setup.md)
- [Betriebsdaten und Repository-Inhalte](docs/data-boundaries.md)
- [Automatisierung, Passwortdatei und systemd-Timer](docs/automation.md)

Die Erfassung zeigt **installierte Pakete**, keine vollständige Container-SBOM,
keinen Nachweis tatsächlich laufender Prozesse und keine automatische
CVE-Betroffenheit. Versionsübergänge haben Beobachtungszeitpunkte; tatsächliche
Installationszeiten werden nicht erfunden.

## Demo

```bash
python3 inventory_web.py --demo
```

Die Demo verwendet acht synthetische Nodes in einem temporären Verzeichnis,
benötigt kein NetBox und führt keine externen Abfragen oder Exporte aus.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Die JavaScript-Prüfungen der Kandidatenauswahl benötigen für die Entwicklung
zusätzlich Node.js (im Serverbetrieb nicht erforderlich):

```bash
node --check web/app.js
node --test tests/test_mapping.cjs
```

Die Tests nutzen temporäre SQLite-Datenbanken und simulierte NetBox-APIs.
Der Test der lokalen SSH-Passwortvermittlung wird übersprungen, wenn die
Ausführungsumgebung Unix-Socket-Bindings verbietet. Die HTTP-Handler werden
zusätzlich ohne geöffneten Netzwerksocket geprüft.

## Struktur

| Datei | Aufgabe |
|---|---|
| `inventory.py` | OpenNMS-Discovery und SNMP-Erfassung |
| `inventory_cycle.py` | Geplante Erfassung und Export bestätigter Zuordnungen |
| `inventory_store.py` | SQLite-Historie und dauerhafte Bindings |
| `netbox_inventory.py` | Import, Kandidaten, Freigabe und Export per CLI |
| `inventory_web.py` | Lokale Weboberfläche und Operator-API |
| `inventory_web_demo.py` | Isolierte Demo-Daten |
| `web/` | HTML, CSS und JavaScript ohne Build-Schritt |
| `examples/` | Ausschließlich synthetische Konfigurationen und Fixtures |
