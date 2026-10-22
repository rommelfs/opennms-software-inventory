NetBox 4.5.3 anbinden – Betrieb auf dem OpenNMS-Server, Version 3.0
================================================================

Diese Erweiterung läuft neben der Erfassung auf deinem OpenNMS-Server.
Sie wurde lokal mit simulierten NetBox-APIs getestet, noch nicht gegen dein NetBox.
Bestehende Inventardateien werden ohne neue SNMP-Abfragen importiert.
Es werden keine Devices/VMs automatisch erstellt oder gelöscht.

Installation: siehe [README](../README.md). Bestehende config.json und state/
beibehalten, insbesondere source_id nicht ändern.

1. Vorhandene Ergebnisse historisieren – kein Netzwerkzugriff
-----------------------------------------------------------
Aus demselben Arbeitsverzeichnis wie bisher, z.B. /root:

  python3 netbox_inventory.py --config config.json import

Das liest state/<source_id>/inventory.json und die Softwaredateien ein.
Auch ältere, erhaltene Snapshot-Dateien für aktuell unerreichbare Nodes werden
importiert. Dateien von inzwischen aus OpenNMS verschwundenen Nodes werden nicht
als aktuelle Hosts neu hinzugefügt. Erneuter Import ist erlaubt und dedupliziert.
Die SQLite-Datei liegt unter state/<source_id>/history.sqlite3.

Wichtig: Die bisher überschriebenen Versionen können nicht rückwirkend
rekonstruiert werden. Vorhandene Dateien sind der Anfang der Historie.
Ab jetzt zeichnet inventory.py jeden neuen erfolgreichen Scan und jeden
Node-Status zusätzlich automatisch in SQLite auf. Kein separater Import nach
jedem neuen Lauf nötig. Während eines laufenden Collectors wartet der Manager
nicht endlos, sondern meldet die belegte Sperre.

2. NetBox-Verbindung ergänzen
----------------------------
In DEINE vorhandene config.json nur den netbox-Abschnitt ergänzen/anpassen:

  "netbox": {
    "url": "https://DEIN-NETBOX",
    "token_env": "NETBOX_TOKEN",
    "auth_scheme": "Token",
    "identity_field": "opennms_node_key",
    "field": "opennms_software_inventory",
    "stale_after_hours": 24
  }

Die URL ist die Basisadresse ohne /api. Bei eigener CA zusätzlich ca_file mit
Pfad zur öffentlichen CA-Datei angeben. TLS-Prüfung bleibt eingeschaltet;
Weiterleitungen werden abgewiesen. Nicht auf HTTP oder verify=false ausweichen.
Für einen klassischen Token auth_scheme=Token verwenden; bei einem v2-Token
(nbt_...) entsprechend Bearer. Das Token selbst gehört nicht in config.json.

Einspielung im Bash-Terminal, ohne Token im Shellverlauf:

  read -rs -p 'NetBox API token: ' NETBOX_TOKEN
  export NETBOX_TOKEN

Für Vorschläge braucht der Benutzer Lesezugriff auf Devices, VMs und IP-Adressen.
Für den Export zusätzlich Änderungsrechte an den gewählten Devices/VMs.
Nur die Anwendung begrenzt die Änderungen auf ihre Felder; NetBox-Objektrechte
sind keine separate Schreibbeschränkung auf einzelne Custom Fields.
Das Anlegen der Custom Fields benötigt einmalig passende Extras-Berechtigungen;
das kann ein Administrator übernehmen und danach einen eingeschränkten Token nutzen.

3. NetBox-Felder prüfen und einmalig bereitstellen
------------------------------------------------
Zuerst ausschließlich lesen, konkrete Vorschau anzeigen:

  python3 netbox_inventory.py --config config.json setup-fields

Anschließend fehlende Felder anlegen:

  python3 netbox_inventory.py --config config.json setup-fields --apply

Bereitgestellt werden an dcim.device UND virtualization.virtualmachine:

  opennms_node_key                 Text, exakte Filterung
  opennms_software_inventory       JSON, Default {}
  software_inventory_observed_at  Datum/Zeit
  software_inventory_status       Text

Statuswerte: current, stale, collection_failed, source_missing. Text vermeidet eine zusätzliche
Choice-Set-Abhängigkeit. Vorhandene Felder werden auf Typ, Objekttypen und bei
Identität exakte Filterung geprüft, aber nicht automatisch umkonfiguriert.
Kein NetBox-Plugin nötig. Bestehende Geräte-/VM-Daten bleiben erhalten.

4. Zuordnungsvorschläge erzeugen – ausschließlich NetBox-GETs
-----------------------------------------------------------

  python3 netbox_inventory.py --config config.json propose --output mapping.json

Vorschläge verwenden:
  - exakten Node-Namen/FQDN gegen Device-/VM-Namen (Groß-/Kleinschreibung und
    abschließender DNS-Punkt werden normalisiert, keine Kurznamensheuristik);
  - DNS-Namen (dns_name) zugewiesener NetBox-IP-Adressen;
  - SNMP-IP gegen IP-Adressen an Device-Interfaces und VM-Interfaces.

Mehrdeutige Namen oder private IPs können mehrere Kandidaten ergeben. Die Datei
nennt Kandidaten, matched_name/matched_ip sowie Site/Tenant. Kein Kandidat wird
automatisch freigegeben: approve ist immer false. Bei genau einem Kandidaten
ist target bereits als Vorschlag gesetzt. Bei mehreren Kandidaten das richtige
Ziel aus candidates wählen und target mit type/id entsprechend setzen.
Nur geprüfte Einträge auf approve=true ändern; die übrigen unverändert lassen.

Für überlappende Netze und mehrere Standorte kann netbox.location_scope die
OpenNMS-Locations auf NetBox-Site/Tenant/VRF beschränken, z.B.:

  "location_scope": {
    "Default": {"site_id": 1, "tenant_id": 2, "vrf_id": null},
    "Branch Berlin": {"site_id": 7, "tenant_id": 2, "vrf_id": 9}
  }

Nur tatsächlich gewünschte Einschränkungen angeben. Ohne Scope erscheint ein
Review-Hinweis. Es gibt keine automatische Gleichsetzung von OpenNMS-Location
und NetBox-Site. Ein passender Hostname allein beweist keine Maschinenidentität.
NetBox-Matching ersetzt nicht die OpenNMS-Discovery und begrenzt deren Hostliste nicht.

5. Geprüfte Zuordnungen lokal bestätigen – NetBox bleibt unverändert
------------------------------------------------------------------
Vorschau/erneute Live-Validierung des bearbeiteten Plans:

  python3 netbox_inventory.py --config config.json confirm --plan mapping.json

Bestätigte Bindings in SQLite speichern:

  python3 netbox_inventory.py --config config.json confirm --plan mapping.json --apply

Dabei wird NetBox erneut gelesen. Veränderte Host-/Kandidatenevidenz wird
abgewiesen. Zwei Nodes dürfen nicht an dasselbe NetBox-Objekt gebunden werden.
Bestehende Bindings werden nicht automatisch auf ein anderes Ziel umgestellt.
Die Bindings enthalten Quelle, Node-ID, feste NetBox-Basisadresse, Device/VM-Typ,
Objekt-ID, Bestätigungszeit und Evidenz. Das spätere Matching benutzt diese
Zuordnung, nicht erneut die Hostnamen. Ein geänderter NetBox-Endpunkt bekommt
keine stillschweigend übernommene Zuordnung.

Eine notwendige Korrektur einer bestehenden Bindung ist in dieser ersten Version
bewusst kein automatischer Rebind-Befehl. Solche Fälle müssen gesondert geklärt
werden; nicht durch Umbenennen von source_id oder Datenbankmanipulation umgehen.

6. Softwarestände übertragen
---------------------------
Vorschau, ausschließlich NetBox lesen:

  python3 netbox_inventory.py --config config.json sync

Danach schreiben:

  python3 netbox_inventory.py --config config.json sync --apply

Der erste Export setzt die Quell-ID am bestätigten Zielobjekt und übernimmt den
Softwarestand. Fremde Quell-IDs/Softwarefelder, doppelte IDs und ein bereits
neuerer Beobachtungszeitpunkt in NetBox führen zu einem sichtbaren Fehler.
Jeder PATCH verändert nur die vier eigenen Custom Fields und fügt eine
Changelog-Nachricht hinzu. Ein GET bestätigt den gespeicherten Stand.
Bei unklarer Antwort erneut sync ausführen: Vor jedem Schreiben wird gelesen.
Dieselbe Beobachtung erneut zu synchronisieren ist ein No-op.

Bleibt die Software bei einem NEUEN Scan gleich, wird die große Paketliste nicht
noch einmal übertragen; lediglich geänderte Metadaten wie der Zeitpunkt können
einen PATCH erzeugen. NetBox selbst kann trotzdem den ganzen Objektzustand im
Changelog speichern. Seine Aufbewahrung sollte zur Datenmenge passen.

Fehlerhafte/fehlende Bindings werden pro Host gemeldet, andere Hosts laufen
weiter. state/<source_id>/netbox-sync.json enthält den letzten Exportbericht;
SQLite speichert die Exportversuche zusätzlich. Synthetische Fixture-Daten
werden nicht exportiert. Es gibt keine Assets-Erstellung oder -Löschung.

7. Weitere Scans und Historie
----------------------------

  python3 inventory.py --config config.json --ssh-interactive
  python3 netbox_inventory.py --config config.json sync
  python3 netbox_inventory.py --config config.json sync --apply

SNMP-Timeouts lassen die anderen Hosts weiterlaufen. Der Collector kann am Ende
Exit 1 liefern, obwohl viele Hosts erfolgreich erfasst wurden. Ein nachfolgender
Export kann die erfolgreichen Beobachtungen trotzdem übertragen; deshalb den
Export nicht blind mit && an einen Collector-Lauf hängen.
Für neue Nodes zuerst importfrei einen neuen Plan erzeugen und diese freigeben.
Der Collector zeichnet sie bereits auf, auch ohne NetBox-Zuordnung.

Die alten Collector-Optionen --sync-netbox/--apply sind ab Version 3.0 gesperrt:
Der Export läuft ausschließlich über den Manager mit bestätigten Bindings.

Beispiel: Versionsübergänge von Node 3 ansehen:

  python3 netbox_inventory.py --config config.json history --node 3

Die Historie kennt baseline, newly_observed, version_changed und
no_longer_observed. Letzteres ist keine bestätigte Deinstallation. Es werden die
Beobachtungszeitpunkte gezeigt, nicht erfundene Installationszeitpunkte.
Identische Stände teilen sich ihre gespeicherte Paketliste; einzelne Beobachtungen
und Fehlschläge bleiben trotzdem erhalten. Auch ein Wechsel A → B → A bleibt
in der Timeline erkennbar. Hostnamenänderungen ändern die historische Identität
nicht; Quelle und Node-ID sind maßgeblich.

Timeouts überschreiben keine gute Softwarebeobachtung. NetBox erhält weiterhin
deren Paketliste und Beobachtungszeit, bei aktueller Fehlermeldung den Status
collection_failed. Ohne neue Erfassung wird nach stale_after_hours stale gezeigt.
Aus OpenNMS verschwundene, bereits gebundene Nodes erhalten source_missing;
ihre NetBox-Assets und letzten Paketstände werden nicht gelöscht.
Ein Lauf, der gar nicht mehr startet, kann diesen Status selbst nicht melden;
dafür ist eine separate Überwachung der Ausführung nötig.

8. Sicherung und Betriebsgrenzen
-------------------------------
Konsistente SQLite-Sicherung bei gestopptem Collector/Manager:

  python3 netbox_inventory.py --config config.json backup --output history-backup.sqlite3

Das Ziel muss neu sein. Die Sicherung enthält Historie UND bestätigte Bindings.
Zusätzlich config.json und die JSON-Ergebnisse sichern; Tokens getrennt behandeln.
Die SQLite-Datei und State-Verzeichnisse müssen auf dauerhaftem lokalen Speicher
liegen. DB/Dateien nicht in Git veröffentlichen. Die Anwendung verwaltet nur
lokale Sperren; keine parallelen Writer mit getrennten State-Verzeichnissen auf
NetBox loslassen. Eine globale Transaktion mit NetBox oder ein API-CAS wird nicht
behauptet. Nur ein Prozess sollte diese vier Custom Fields verwalten.

Ändern sich bei einer gebundenen Quelle bekannte foreignSource/foreignId oder
Node-Erstellungszeit, stoppt deren Export zur Prüfung. Ältere Snapshots enthalten
noch keine Node-Erstellungszeit. Ein Hardware-/VM-Austausch unter identischer
OpenNMS-Identität ist ohne zusätzliche Identitätsdaten nicht sicher erkennbar.
source_id und Node-IDs dürfen deshalb nicht als beliebig wiederverwendbare Namen
behandelt werden. Eine OpenNMS-Neuinstallation ist ein bewusster Quellenwechsel.

Es wurde keine Zeitplanung eingerichtet. Für unbeaufsichtigte Erfassung ist
weiterhin ein geeigneter SSH-Zugang nötig; --ssh-interactive fragt einmal pro
Lauf ein Passwort ab und ist kein unbeaufsichtigter Scheduler-Modus.

Die REST-Verträge der Custom Fields wurden am NetBox-Quellcode-Tag v4.5.3 geprüft:
https://github.com/netbox-community/netbox/blob/v4.5.3/netbox/extras/api/serializers_/customfields.py
https://netboxlabs.com/docs/netbox/v4.5/integrations/rest-api/

Noch nicht enthalten: Web-Oberfläche, automatisch angelegte Assets, automatische
Rebindings, ein NetBox-Paketmodell/Plugin oder die Bearings-Integration. Die
SQLite-Historie und expliziten Quellbindungen sind deren Anschlussgrundlage.
