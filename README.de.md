# <img src="web/static/favicon.svg" alt="" width="36" align="top"> Shelfripper

**CD einlegen, getaggtes Album bekommen.** Zwei kleine Docker-Container für Windows: Der eine rippt jede Audio-CD, die im Laufwerk landet, der andere liefert eine Webseite zum Prüfen, Taggen und Archivieren.

[English version](README.md)

![Albumansicht mit laufendem Rip](docs/album.png)

*Die Alben in den Bildern sind erfunden.*

## Was es kann

- **Rippt von selbst.** CD einlegen, sie wird fehlerkorrigierend gelesen (cdparanoia), als FLAC oder MP3 gespeichert und ausgeworfen.
- **Schlägt die CD bei MusicBrainz nach.** Unbekannte CDs sind im Regal markiert.
- **Taggt aus Discogs.** Veröffentlichung suchen, Titellängen vergleichen, Interpret, Titel, Jahr, Label und Katalognummer übernehmen.
- **Cover.** Suche über [COV](https://covers.musichoarders.xyz/), Datei hochladen oder Bild einfügen.
- **Alben mit mehreren CDs** liegen als `Interpret/Album/CD1`, `CD2`, … und erscheinen als ein Album. Eine CD lässt sich per Drag & Drop auf ein Album ziehen.
- **Fortschritt live** mit Titelzähler, dazu ein Knopf zum Abbrechen mit Auswurf.
- **Archiv.** Fertige Alben in einen lokalen Ordner oder auf eine SMB-Freigabe verschieben, auf Wunsch per SHA-256 geprüft: einzeln, alle auf einmal oder automatisch nach jedem erkannten Rip.
- **Download als ZIP**, Abspielknopf je Titel, Jahr vor dem Albumordner (`1992 - Album`) zuschaltbar, Oberfläche auf Deutsch und Englisch.

## Voraussetzungen

- Windows 10/11 (x64) mit [Docker Desktop](https://www.docker.com/products/docker-desktop/) und WSL2
- Ein CD/DVD-Laufwerk am USB
- [usbipd-win](https://github.com/dorssel/usbipd-win), das das USB-Laufwerk an WSL2 durchreicht

Unter Linux sollte es mit `docker compose up -d --build` ebenfalls laufen, wenn das Laufwerk `/dev/sr0` ist; die `.bat`-Dateien sind nur für Windows. Getestet ist das nicht.

## Loslegen

1. Dieses Repository herunterladen oder klonen, zum Beispiel nach `D:\Docker\shelfripper`.
2. Laufwerk anschließen und **`start.bat`** doppelklicken. Sie fordert Administratorrechte an, listet die USB-Geräte auf und fragt nach der BUSID des Laufwerks (zum Beispiel `2-3`). Danach baut und startet sie beide Container. Der erste Bau dauert ein paar Minuten.
3. <http://localhost:8080> öffnen.
4. CD einlegen.

Nach einem Neustart oder nach dem Umstecken des Laufwerks `start.bat` erneut ausführen, damit das Laufwerk wieder an WSL2 durchgereicht wird.

| Datei | Zweck |
| --- | --- |
| `start.bat` | Reicht das Laufwerk durch und startet alles |
| `start-web.bat` | Baut nur die Weboberfläche neu; ein laufender Rip bleibt ungestört |
| `update.bat` | Baut beide Container neu; nur ausführen, wenn gerade keine CD gerippt wird |

## Einstellungen

![Einstellungen](docs/settings.png)

- **Format:** FLAC oder MP3 (LAME V0). Gilt ab der nächsten CD.
- **Discogs-Token:** kostenlos unter [discogs.com/settings/developers](https://www.discogs.com/settings/developers) mit „Generate new token“. Es wird beim Speichern geprüft.
- **Archivordner:** ein Ordner auf dem Rechner (`E:\Musikarchiv`) oder eine Netzwerkfreigabe (`\\server\freigabe\ordner`) mit Benutzer und Kennwort. Ein lokaler Ordner gilt, nachdem `update.bat` einmal gelaufen ist; eine Freigabe sofort. Lässt sich der Servername im Container nicht auflösen, hilft die IP-Adresse.

## Was wo liegt

| Ordner | Inhalt |
| --- | --- |
| `musik/` | Die gerippten Alben: `Interpret/Album/01 - Titel.flac` |
| `archiv/` | Standard-Archivordner |
| `config/` | `settings.json` und das Protokoll `web.log` |

In `config/settings.json` stehen das Discogs-Token und das Kennwort der Freigabe **im Klartext**. Der Ordner ist von Git ausgenommen; bitte nicht weitergeben.

## Gut zu wissen

- Die Weboberfläche ist nur vom Rechner selbst erreichbar (`127.0.0.1:8080`). Sie hat keine Anmeldung und gehört so nicht ins Netz.
- Abfragen gehen an MusicBrainz und Discogs. Die Coversuche öffnet COV in einem eigenen Fenster.
- Ordner lassen sich nicht umbenennen, solange der Explorer oder ein Player sie offen hat. Die Anwendung versucht es später erneut.
- Rippe nur CDs, die du nach dem für dich geltenden Recht kopieren darfst.

## Aufbau

- `Dockerfile`, `rip.sh`, `abcde.conf`: der Ripper, Debian mit [abcde](https://abcde.einval.com/), cdparanoia, flac und lame. `rip.sh` überwacht das Laufwerk und meldet den Fortschritt in `musik/.cdripper/status.json`.
- `web/`: die Weboberfläche, Python mit Flask und mutagen in einer Datei (`app.py`), dazu eine HTML-Datei ohne Build-Schritt.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
