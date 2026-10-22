Inventory Web 3.1 – Zuordnungen im Browser statt mapping.json bearbeiten
======================================================================

Die Oberfläche läuft auf dem OpenNMS-Server neben den bisherigen Werkzeugen.
Sie liest dieselbe config.json und dieselbe SQLite-Historie. Keine zusätzlichen
Python-Pakete, kein Frontend-Build und keine externen JavaScript-/Font-Dienste.

1. Mit Git installieren
------------------------
Installation und Updates sind in der [README](../README.md) beschrieben.
Den Checkout vollständig klonen; das Verzeichnis web gehört zur Anwendung.
Konfiguration und Inventardaten bleiben außerhalb des Checkouts.

2. Oberfläche starten
---------------------
Den NetBox-Token wie für das CLI in NETBOX_TOKEN bereitstellen, oder die in
config.json benannte Token-Umgebungsvariable verwenden. Er bleibt auf dem Server.
Die OpenNMS-API-/SSH-Zugangsdaten braucht die Oberfläche selbst nicht:
Sie arbeitet mit bereits erhobenen Inventardaten.

  python3 inventory_web.py --config config.json --state ./state

Beim Start werden vorhandene Ergebnisse importiert, soweit die lokale Sperre
frei ist. Bei laufender Erfassung oder einem zurückgestellten Import bleibt die
Oberfläche verfügbar und zeigt einen Hinweis. Nach Ende der Erfassung
„Erfassung einlesen“ verwenden. Neuere Collector-Versionen schreiben bereits
direkt in die SQLite-Historie; der Import ist wiederholbar und dedupliziert.

Die Konsole gibt einen Link dieser Form aus:

  http://127.0.0.1:8765/#token=<Sitzungstoken>

Der Server bindet ausschließlich an 127.0.0.1, nicht an eine öffentliche Adresse.
Er läuft im Vordergrund; Ctrl+C beendet ihn. Ein Neustart erzeugt einen neuen
Zugangslink, ohne die Inventardaten oder Bindings zu verlieren.

3. Auf deinem Rechner per SSH-Tunnel öffnen
------------------------------------------
In einem zweiten Terminal auf DEINEM Rechner, nicht auf ccq:

  ssh -N -L 8765:127.0.0.1:8765 root@ccq

Statt root@ccq kannst du deinen üblichen SSH-Zugang/Alias zum OpenNMS-Server
verwenden. Dies ist der normale Server-SSH-Zugang (typischerweise Port 22),
nicht die Karaf-Verbindung auf Port 8101.

Danach den vollständigen Konsolenlink mit #token=... im Browser deines Rechners
öffnen. Falls Port 8765 bereits belegt ist, die Oberfläche mit --port 8766
starten und im Tunnel auf beiden Seiten denselben Port 8766 verwenden.
Host-/Origin-Prüfung erwartet denselben Port am Browser und am Server.

Der Sitzungstoken autorisiert den lokalen Arbeitsbereich. Er wird nicht als
URL-Query an den Server gesendet; der Browser übernimmt ihn aus dem Fragment und
verwendet ihn für die API. Die Daten-API verlangt ihn für jeden Aufruf.
NetBox-Token, API-Passwörter und SSH-Passwörter werden nicht zum Browser geschickt.
Der Zugang ist für einen Operator über den SSH-Tunnel gedacht, nicht als
öffentlich erreichbarer Mehrbenutzerdienst.

4. Zuordnen ohne JSON-Datei
--------------------------
- „NetBox abgleichen“: liest Devices, VMs und zugewiesene IP-Adressen vollständig
  aus NetBox und erzeugt die Kandidaten. Noch keine NetBox-Schreibzugriffe.
- Suche und Filter: nach Node, IP, NetBox-Kandidat oder DNS-Namen suchen;
  offene, bestätigte, eindeutige oder mehrdeutige Fälle anzeigen.
- Pro Node ein Ziel aus der Auswahlliste wählen. Name, Device/VM-Typ, Objekt-ID,
  Site, Tenant, IP-Adressen/VRFs und DNS-Namen stehen beim Kandidaten.
- Die Chips „Name“, „DNS“ und „IP“ zeigen die Art der Übereinstimmung.
  Bereits anderweitig gebundene Ziele sind als belegt erkennbar und gesperrt.
- Die gewünschten Zeilen per Checkbox auswählen.
- „Auswahl bestätigen“ zeigt die Zahl und Wirkung der ausgewählten Zuordnungen.
  Nach Bestätigung werden die Ziele erneut live geprüft und lokal gespeichert.
  NetBox bleibt bei diesem Schritt unverändert.

Die Namenserkennung vergleicht Node-label und, bei neuen Erfassungen vorhanden,
sysName mit dem NetBox-Objektnamen und mit dns_name zugewiesener NetBox-IP-Adressen.
Groß-/Kleinschreibung und ein abschließender DNS-Punkt werden normalisiert.
Es werden keine zusätzlichen externen DNS-Abfragen ausgelöst. Es gibt keine
heuristische Gleichsetzung beliebiger Kurznamen mit einem FQDN.

Mehrere Kandidaten bleiben eine bewusste Auswahl. Scope-Regeln aus
netbox.location_scope gelten weiter (siehe [NetBox-Anleitung](netbox-setup.md)). Ein einzelner Treffer
bedeutet nicht automatisch eine freigegebene Zuordnung. Bei fehlenden Kandidaten
NetBox-Namen, DNS- und IP-Zuordnungen prüfen. Diese Version erzeugt keine Assets
und bietet kein ungeprüftes manuelles Binding zu beliebigen Objekt-IDs.

5. Eindeutige Treffer gesammelt vorauswählen
------------------------------------------
„Eindeutige auswählen“ setzt die Checkboxen der aktuell sichtbaren Zeilen, wenn:
- genau ein Kandidat vorliegt,
- Name oder DNS UND die SNMP-IP übereinstimmen,
- kein anderes Node denselben Kandidaten beansprucht,
- das Ziel keine widersprechende Quell-ID oder lokale Bindung trägt,
- bei privaten/nichtglobalen IPs ein nichtleerer Location-Scope konfiguriert ist,
- es sich um echte und noch nicht gebundene Daten handelt.

Anschließend die vorausgewählten Zeilen ansehen und gesammelt bestätigen.
Der Knopf speichert selbst keine Bindings. Die übrigen Fälle können einzeln
ausgewählt werden. Fehlende Scope-Regeln erklären, warum trotz passender
privater IP nicht alles für die Sammelauswahl freigegeben ist.

Bestehende Zuordnungen werden angezeigt und für automatische Änderungen gesperrt.
Umbenennungen führen weiterhin zu derselben Node-ID/NetBox-Objekt-ID-Verbindung.
Rebindings werden nicht stillschweigend durchgeführt.

6. Software und Historie ansehen
-------------------------------
Ein Klick auf den Node-Namen oder auf den Detailpfeil öffnet ein Fenster mit:
- durchsuchbarer Paketliste, Version und Architektur,
- Versionshistorie mit Beobachtungszeitpunkten,
- neu beobachteten oder nicht mehr beobachteten Paketen.

Unter „Software & Historie“ gibt es außerdem die Übersicht aller aktuellen
Nodes mit Paketanzahl, letztem erfolgreichen Beobachtungszeitpunkt und
Erfassungsstatus. Ein Timeout entfernt keinen alten Softwarestand.

7. NetBox-Export in der Oberfläche
---------------------------------
Die vier Custom Fields müssen einmalig bereitgestellt sein, wie bereits für
Version 3.0 beschrieben. Bei Bedarf im Terminal:

  python3 netbox_inventory.py --config config.json setup-fields
  python3 netbox_inventory.py --config config.json setup-fields --apply

Danach im Browser „NetBox-Export“ wählen:
- „Exportvorschau erstellen“ liest die bestätigten Ziele und zeigt pro Node,
  welche Inventarfelder geändert würden. Es wird noch nichts geschrieben.
- „Änderungen nach NetBox schreiben“ ist erst nach einer geeigneten Vorschau
  aktiv. Die folgende Bestätigung startet die tatsächlichen PATCH-Aufrufe.
- Ändern sich lokale Erfassung oder Bindings seit der Vorschau, wird der Export
  abgewiesen. Auch die NetBox-Ergebnisse werden vor dem Schreiben neu geprüft.
  Dann eine neue Vorschau erstellen.
- Eine erneute Synchronisierung derselben Beobachtung ist weiterhin ein No-op.
  Bei Verlust einer Antwort wird der gespeicherte Stand beim nächsten Versuch
  wieder gelesen, statt zusätzliche Assets oder Historieneinträge zu erzeugen.

Es wird kein Device und keine VM erstellt oder gelöscht. Export und lokale
Zuordnung bleiben getrennte Aktionen. Die lokalen CLI-Sperren gelten auch für
Web-Aktionen: eine Erfassung oder ein anderer Manager kann diese nicht parallel
überschreiben. Die Weboberfläche selbst führt nur eine Aktion gleichzeitig aus.
Sie ersetzt keine globale NetBox-Transaktion oder atomare Compare-and-Swap-API.

8. Demo und technische Prüfung
------------------------------
Ohne Konfiguration, ohne NetBox und ohne externe Netzwerkzugriffe:

  python3 inventory_web.py --demo

Die Demo enthält acht synthetische Nodes, DNS-Treffer, einen mehrdeutigen Fall,
einen Timeout, eine bereits vorhandene Zuordnung und Versionswechsel.
Sie nutzt ein temporäres Verzeichnis; Bindings und NetBox-Export sind gesperrt.
Auch bei der Demo gilt der ausgegebene Sitzungstoken und gegebenenfalls der Tunnel.

Tests:
  python3 -m unittest discover -s tests -v

Die Tests prüfen echte SQLite-Dateien, den HTTP-Handler einschließlich
Authentifizierung/Origin, DNS-Matching, Auswahllogik, Live-Neuvalidierung und
Exportvorschau. Die HTTP-Handler können ohne geöffneten Netzwerksocket getestet
werden. Der bestehende Test der SSH-Passwortvermittlung wird in Sandboxes
übersprungen, die Unix-Socket-Bindings nicht erlauben.

Verifizierungsgrenze dieser Lieferung: JavaScript-Syntax und Backend-Tests sind
geprüft. Ein grafischer Browserstart war in der Entwicklungsumgebung nicht
möglich; deshalb gibt es noch keine abgeschlossene visuelle Browserabnahme.
Die Anbindung an dein NetBox 4.5.3 ist weiterhin auf ccq live zu prüfen.
