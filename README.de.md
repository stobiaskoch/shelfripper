# <img src="web/static/favicon.svg" alt="" width="36" align="top"> ShelfRipper

**CD einlegen, getaggtes Album bekommen.** Zwei kleine Docker-Container: Der eine rippt jede Audio-CD, die im Laufwerk landet, der andere liefert eine Webseite zum Prüfen, Taggen und Archivieren. Läuft unter Windows (Docker Desktop) und unter Debian, auch als Debian in einem Proxmox-LXC-Container.

[English version](README.md)

![Albumansicht mit laufendem Rip](docs/screenshot-album.png)

*Die Alben in den Bildern sind erfunden.*

## Was es kann

- **Rippt von selbst.** CD einlegen, sie wird fehlerkorrigierend gelesen (cdparanoia), als FLAC oder MP3 gespeichert und ausgeworfen.
- **Schlägt die CD bei MusicBrainz nach.** Unbekannte CDs sind im Regal markiert.
- **Taggt aus Discogs.** Veröffentlichung suchen, Titellängen vergleichen, Interpret, Titel, Jahr, Label und Katalognummer übernehmen. Die Wahl wird je CD gemerkt: Kommt dieselbe CD wieder ins Laufwerk, wird sie sofort mit den Discogs-Daten getaggt.
- **Cover.** Nach jedem erkannten Rip automatisch aus dem [Cover Art Archive](https://coverartarchive.org/); sonst Suche über [COV](https://covers.musichoarders.xyz/), Datei hochladen oder Bild einfügen.
- **Alben mit mehreren CDs** liegen als `Interpret/Album/CD1`, `CD2`, … und erscheinen als ein Album. Eine CD lässt sich per Drag & Drop auf ein Album ziehen.
- **Fortschritt live** mit Titelzähler, dazu ein Knopf zum Abbrechen mit Auswurf.
- **Archiv.** Fertige Alben in einen lokalen Ordner oder auf eine SMB-Freigabe verschieben, auf Wunsch per SHA-256 geprüft: einzeln, alle auf einmal oder automatisch nach jedem erkannten Rip.
- **Download als ZIP oder Löschen** direkt in der Albumliste, Abspielknopf je Titel, Jahr vor dem Albumordner (`1992 - Album`) zuschaltbar, Oberfläche auf Deutsch und Englisch.

## Installation unter Windows

Du brauchst:

- Windows 10/11 (x64) mit [Docker Desktop](https://www.docker.com/products/docker-desktop/) und WSL2
- Ein CD/DVD-Laufwerk am USB
- [usbipd-win](https://github.com/dorssel/usbipd-win), das das USB-Laufwerk an WSL2 durchreicht

Dann:

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

## Installation unter Debian

Du brauchst Docker mit dem Compose-Plugin und git sowie ein CD/DVD-Laufwerk, das als `/dev/sr0` erscheint:

```
ls -l /dev/sr0
```

Dann:

```
git clone https://github.com/stobiaskoch/shelfripper.git
cd shelfripper
cp .env.example .env
docker compose up -d --build
```

`http://<server>:8080` öffnen und eine CD einlegen.

- **`.env`:** `WEB_BIND=0.0.0.0` macht die Weboberfläche im ganzen Netz erreichbar (sie hat keine Anmeldung). Ist Port 8080 schon belegt, `WEB_PORT` ändern.
- **Rechte:** Meldet `docker` „permission denied“, den Benutzer in die Gruppe `docker` aufnehmen (`sudo usermod -aG docker $USER`, danach neu anmelden) oder `sudo` verwenden.
- **Aktualisieren:** `git pull && docker compose up -d --build`, am besten, während keine CD gerippt wird. `docker compose up -d --build web` aktualisiert nur die Weboberfläche.
- **Laufwerk umgesteckt:** Der Ripper bekommt `/dev/sr0` beim Start, danach also `docker compose restart cdripper`.
- **Lokaler Archivordner:** `ARCHIVE_DIR=/pfad/zum/archiv` in die `.env` eintragen und `docker compose up -d` ausführen. Eine SMB-Freigabe stellst du wie unter Windows in der Weboberfläche ein.

### Debian in einem Proxmox-LXC-Container

Docker in einem LXC-Container sieht das USB-Laufwerk nicht von selbst: Der Container zeigt es zwar in `lsblk`, aber `/dev/sr0` fehlt. Auf dem **Proxmox-Host** durchreichen (`105` durch die eigene Container-ID ersetzen):

```
pct set 105 -dev0 /dev/sr0 -dev1 /dev/sg0
pct reboot 105
```

oder in der Proxmox-Weboberfläche: Container → *Resources* → *Add* → *Device Passthrough*. Bei Proxmox-Versionen vor 8.1 stattdessen diese Zeilen in `/etc/pve/lxc/105.conf` eintragen und den Container neu starten:

```
lxc.cgroup2.devices.allow: c 11:0 rwm
lxc.cgroup2.devices.allow: c 21:0 rwm
lxc.mount.entry: /dev/sr0 dev/sr0 none bind,optional,create=file
lxc.mount.entry: /dev/sg0 dev/sg0 none bind,optional,create=file
```

Die Gerätenummern prüfst du auf dem Host mit `ls -l /dev/sr0 /dev/sg*`; hat der Host weitere SCSI-Geräte, heißt `sg0` womöglich anders. Der Container braucht außerdem Docker-Unterstützung (`nesting=1`, bei unprivilegierten Containern meist auch `keyctl=1`). Auswerfen klappt dort ebenfalls: Scheitert das normale `eject`, schickt der Ripper den Auswurfbefehl direkt ans Laufwerk.

## Einstellungen

![Einstellungen](docs/screenshot-settings.png)

- **Format:** FLAC oder MP3 (LAME V0). Gilt ab der nächsten CD.
- **Discogs-Token:** kostenlos unter [discogs.com/settings/developers](https://www.discogs.com/settings/developers) mit „Generate new token“. Es wird beim Speichern geprüft.
- **Archivordner:** ein Ordner auf dem Rechner (`E:\Musikarchiv`) oder eine Netzwerkfreigabe (`\\server\freigabe\ordner`) mit Benutzer und Kennwort. Ein lokaler Ordner gilt, nachdem `update.bat` einmal gelaufen ist; eine Freigabe sofort. Lässt sich der Servername im Container nicht auflösen, hilft die IP-Adresse. Unter Linux kommt ein lokaler Archivordner in die `.env` (siehe oben).

## Was wo liegt

| Ordner | Inhalt |
| --- | --- |
| `musik/` | Die gerippten Alben: `Interpret/Album/01 - Titel.flac` |
| `archiv/` | Standard-Archivordner |
| `config/` | `settings.json`, die gemerkten Discogs-Ausgaben `discogs-cds.json` und das Protokoll `web.log` |

In `config/settings.json` stehen das Discogs-Token und das Kennwort der Freigabe **im Klartext**. Der Ordner ist von Git ausgenommen; bitte nicht weitergeben.

## Gut zu wissen

- Ohne `.env` ist die Weboberfläche nur vom Rechner selbst erreichbar (`127.0.0.1:8080`); `WEB_BIND` öffnet sie fürs Netz. Sie hat keine Anmeldung, also nur in einem vertrauenswürdigen Heimnetz freigeben und nie ins Internet.
- Abfragen gehen an MusicBrainz, das Cover Art Archive und Discogs. Die Coversuche öffnet COV in einem eigenen Fenster.
- Unter Windows lassen sich Ordner nicht umbenennen, solange der Explorer oder ein Player sie offen hat. Die Anwendung versucht es später erneut.
- Rippe nur CDs, die du nach dem für dich geltenden Recht kopieren darfst.

## Aufbau

- `Dockerfile`, `rip.sh`, `abcde.conf`: der Ripper, Debian mit [abcde](https://abcde.einval.com/), cdparanoia, flac und lame. `rip.sh` überwacht das Laufwerk und meldet den Fortschritt in `musik/.cdripper/status.json`.
- `web/`: die Weboberfläche, Python mit Flask und mutagen in einer Datei (`app.py`), dazu eine HTML-Datei ohne Build-Schritt.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
