# Periodischer Betrieb auf ccq

Die mitgelieferte systemd-Service- und Timer-Unit führt täglich eine Erfassung
über OpenNMS aus und exportiert anschließend die bereits bestätigten
Zuordnungen nach NetBox. Sie benutzt weiterhin `/root/config.json` und
`/root/state`. Neue Nodes werden automatisch aus OpenNMS entdeckt und lokal
historisiert; ihre NetBox-Zuordnung bestätigst du in der Weboberfläche.
Die Oberfläche muss für den täglichen Lauf nicht gestartet sein.

## Anmeldung bei OpenNMS 35.0.5

Für Karaf auf dem Core ist SSH-Public-Key-Anmeldung erst mit Horizon 36.0.2
als Neuerung dokumentiert ([Release Notes, NMS-19953](https://github.com/OpenNMS/opennms/releases/tag/opennms-36.0.2-1)).
Für den Betrieb mit 35.0.5 benutzt die Vorlage deshalb ein Passwort aus einer
explizit angegebenen Datei. Ein OpenNMS-Upgrade ist dafür nicht erforderlich.

Das Passwort liegt dabei dauerhaft als Klartext auf dem Server in einer nur
für root lesbaren Datei. Der Collector lehnt gruppen- oder fremdlesbare Dateien,
falsche Eigentümer, Symlinks, leere Dateien und mehrzeilige Passwörter ab.
Datei und Verzeichnis gehören nicht ins Git-Repository. Das Passwort gelangt
weder in die SSH-Kommandozeile noch in Umgebungsvariablen oder Journald-Logs;
der bestehende private ASKPASS-Kanal übergibt es an OpenSSH.
Die interaktive Anmeldung funktioniert weiterhin mit `--ssh-interactive`.

## Einmalige Vorbereitung

Auf ccq als root:

```bash
cd /opt/opennms-software-inventory
git pull --ff-only
install -d -m 700 /etc/opennms-software-inventory
(umask 077; touch /etc/opennms-software-inventory/credentials.env /etc/opennms-software-inventory/karaf-password)
chmod 600 /etc/opennms-software-inventory/credentials.env /etc/opennms-software-inventory/karaf-password
nano /etc/opennms-software-inventory/credentials.env
```

In `credentials.env` die API-Zugangsdaten hinterlegen. Die Namen müssen den
`username_env`, `password_env` und `token_env`-Werten deiner config.json entsprechen.
Die Standardnamen sind:

```ini
OPENNMS_USERNAME='DEIN_API_BENUTZER'
OPENNMS_PASSWORD='DEIN_API_PASSWORT'
NETBOX_TOKEN='DEIN_NETBOX_TOKEN'
```

Nur Zuweisungen, keine `export`-Anweisungen, Shell-Kommandos oder Variablenverweise
verwenden. Die Datei liest systemd als `EnvironmentFile`, nicht als Shell-Skript.
Die Werte oben sind Platzhalter. Passwörter mit Anführungszeichen entsprechend
der [EnvironmentFile-Syntax](https://www.freedesktop.org/software/systemd/man/251/systemd.exec.html#EnvironmentFile=)
eintragen.

Danach:

```bash
nano /etc/opennms-software-inventory/karaf-password
```

Hier nur das SSH-Passwort für `admin@localhost:8101` als eine Zeile eintragen,
keinen Variablennamen. Ein abschließender Zeilenumbruch ist erlaubt.
Das SSH-Ziel bleibt dein `ssh_target` aus config.json. Bei einem SSH-Alias
benutzt der Service als root dieselbe `/root/.ssh/config` und dieselben
bekannten Hostschlüssel wie dein bisheriger Lauf. Der Hostschlüssel muss
bereits bekannt sein; die strikte Prüfung bleibt aktiviert.

Die NetBox-Custom-Fields müssen bereits eingerichtet sein und der Token muss
die benötigten Lese- und Änderungsrechte haben. Der tägliche Lauf erstellt
keine Felder und bestätigt keine neuen Zuordnungen.

## Zuerst einen vollständigen Probelauf starten

Dieser Lauf erfasst und historisiert echte Softwarestände; der NetBox-Export
bleibt eine Vorschau, weil `--apply` fehlt. API-Zugänge werden wie später im
Service aus der Environment-Datei geladen:

```bash
systemd-run --unit=opennms-inventory-check --wait --pipe --collect \
  --property=EnvironmentFile=/etc/opennms-software-inventory/credentials.env \
  --property=UMask=0077 \
  /usr/bin/python3 /opt/opennms-software-inventory/inventory_cycle.py \
  --config /root/config.json --state /root/state \
  --ssh-password-file /etc/opennms-software-inventory/karaf-password
```

Bei unerreichbaren Hosts läuft die Erfassung weiter. Der Zyklus meldet dann
Exitcode 1, führt die Exportvorschau aber trotzdem aus. Ein falsches SSH-Passwort,
fehlgeschlagene Discovery oder ein unvollständiger Lauf verhindert den Export.
Vor dem nächsten Schritt bei Fehlern das Log und `cycle.status.json` prüfen.

## Service und Timer installieren

Der Service enthält `--apply`: Ab jetzt werden bestätigte Ziele tatsächlich
aktualisiert. Die Units aktivieren sich nicht durch einen Git-Checkout:

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

Standard: täglich ab 02:00 Uhr mit bis zu zehn Minuten Zufallsverzögerung, in der
Serverzeitzone. `Persistent=true` holt einen während eines ausgeschalteten oder
gestoppten Timers verpassten Termin beim Aktivieren nach. Ein schon laufender
Service wird vom Timer nicht nochmals gestartet.
Siehe [systemd.timer](https://www.freedesktop.org/software/systemd/man/251/systemd.timer.html).

## Häufigkeit ändern

Beispiel: alle sechs Stunden, ab 00:00 Uhr:

```bash
systemctl edit opennms-software-inventory.timer
```

Den Drop-in-Inhalt setzen:

```ini
[Timer]
OnCalendar=
OnCalendar=*-*-* 00,06,12,18:00:00
```

Danach:

```bash
systemctl daemon-reload
systemctl restart opennms-software-inventory.timer
systemctl list-timers opennms-software-inventory.timer
```

Das leere `OnCalendar=` entfernt den bisherigen Tagesplan, bevor der neue
gesetzt wird. Mit täglich beginnen und erst anhand der Laufzeit erhöhen:
Die SNMP-Walks laufen nacheinander, und Timeouts können den Lauf verlängern.
Der Timer wartet auf einen laufenden Service. Mehrere manuelle Zyklusaufrufe
für dieselbe Quelle verhindert zusätzlich `.cycle.lock`.

## Betrieb und Fehler

```bash
systemctl status opennms-software-inventory.service
journalctl -u opennms-software-inventory.service --since today
systemctl list-timers opennms-software-inventory.timer
```

Zusätzlich stehen im Verzeichnis `/root/state/<source_id>/`:

- `cycle.status.json`: Status des letzten Zyklus, Schritte und Exitcodes.
- `inventory.json`: letzter Erfassungslauf inklusive Hostfehlern.
- `netbox-sync.json`: letzter Export bzw. letzte Exportvorschau.
- `history.sqlite3`: vollständige lokale Beobachtungs- und Exporthistorie.

Exitcodes des Zyklus:

| Code | Bedeutung |
|---|---|
| 0 | Erfassung und Export/Vorschau abgeschlossen |
| 1 | Einzelne Hostabfragen fehlgeschlagen; abgeschlossene Erfassung und Export/Vorschau trotzdem ausgeführt |
| 2 | Fehler bei Vorbereitung, Gesamterfassung oder Export; Details im Status/Log |

Exitcode 1 wird von systemd bewusst als Fehler sichtbar gemacht. Das bedeutet
nicht, dass die erfolgreichen Hosts nicht übertragen wurden. Fehlende
Zuordnungen sind kein Exportfehler: `sync --confirmed-only` protokolliert sie
als `skipped`, ohne Assets anzulegen oder die Zuordnung zu erraten. Fehler
an bereits gebundenen Hosts bleiben dagegen sichtbar.

Die bisherigen Schutzmechanismen gelten weiter: Keine Übernahme fremder
Identitäten und kein Überschreiben neuerer Beobachtungen in NetBox. Ein
fehlgeschlagener Hostscan behält seinen letzten erfolgreichen Softwarestand;
der Export kann dessen Zustand als veraltet bzw. fehlgeschlagen kennzeichnen.
Ein abgebrochener oder alter Gesamtreport wird nicht als neuer Lauf exportiert.

Während der Collector oder Export die Betriebsdaten sperrt, lehnen schreibende
UI-Aktionen einen parallelen Abgleich ab. Danach erneut versuchen. Nach einem
neuen Scan „NetBox abgleichen“ benutzen, wenn du neue Nodes zuordnen willst.
Regelmäßig die SQLite-Datenbank mit dem vorhandenen `backup`-Befehl sichern.

Für Codeupdates den Timer vorübergehend stoppen und einen laufenden Service
zuerst fertig werden lassen. Danach `git pull --ff-only`. Änderungen an den
Unit-Vorlagen werden erst durch erneutes Kopieren und `daemon-reload` wirksam;
eigene systemd-Drop-ins bleiben erhalten. Zum Abschalten:

```bash
systemctl disable --now opennms-software-inventory.timer
```

Das stoppt künftige Termine. Ein bereits laufender Erfassungsvorgang bleibt
aktiv und kann regulär fertig werden.
