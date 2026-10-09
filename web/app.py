"""Weboberflaeche fuer den CD-Ripper: Rips anzeigen, Tags bearbeiten, Discogs-Suche."""

import base64
import hashlib
import shutil
import sqlite3
import tempfile
import threading
import zipfile
import io
import ipaddress
import socket
import binascii
import json
import logging
import logging.handlers
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_file, Response
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, ID3NoHeaderError, APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TXXX
from mutagen.mp3 import MP3
from werkzeug.exceptions import HTTPException

try:  # Zugriff auf Netzwerkfreigaben (SMB) fuer das Archiv; fehlt die Bibliothek, gehen nur lokale Ordner
    import smbclient
    import smbclient.path
except ImportError:
    smbclient = None

try:  # zum Verkleinern grosser Cover; ohne Pillow werden Bilder unveraendert uebernommen
    from PIL import Image
except ImportError:
    Image = None

OUTPUT = Path(os.environ.get("OUTPUT_DIR", "/output")).resolve()
CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
DISCOGS_API = os.environ.get("DISCOGS_API", "https://api.discogs.com").rstrip("/")
USER_AGENT = "shelfripper/1.0 (https://github.com/stobiaskoch/shelfripper)"
MUSICBRAINZ_API = os.environ.get("MUSICBRAINZ_API", "https://musicbrainz.org/ws/2").rstrip("/")
COVERART_API = os.environ.get("COVERART_API", "https://coverartarchive.org").rstrip("/")
STATUS_FILE = OUTPUT / ".cdripper" / "status.json"
FORMAT_FILE = OUTPUT / ".cdripper" / "format"  # liest der Ripper vor jeder CD
CANCEL_FILE = OUTPUT / ".cdripper" / "cancel"  # darauf achtet der Ripper waehrend eines Rips
ACTIVE_STATES = ("ripping", "converting")      # Auslesen der CD bzw. Umwandeln in MP3
ARCHIVE = Path(os.environ.get("ARCHIVE_DIR", "/archive"))      # im Container eingebundener Archivordner
ARCHIVE_ACTIVE = os.environ.get("ARCHIVE_HOST_PATH", "").strip()  # Windows-Pfad dazu ("" = Standardordner "archiv")
SMB_PORT = int(os.environ.get("SMB_PORT", "445"))
ARCHIVE_WANTED_FILE = CONFIG / "archive_path.txt"                 # gewuenschter Pfad; lesen die .bat-Dateien beim Start
YEAR_FILE = OUTPUT / ".cdripper" / "year_prefix"  # "1" = Jahr vor den Albumordner stellen; liest auch der Ripper
YEAR_PREFIX = re.compile(r"^(\d{4}) - (.+)$")
FORMATS = ("flac", "mp3")
AUDIO_SUFFIXES = (".flac", ".mp3")
BUSY_SECONDS = 300  # ohne Statusdatei: so lange nach dem letzten Titel gilt ein unvollstaendiges Album als "in Arbeit"

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Warnungen und Fehler zusaetzlich in config/web.log schreiben, damit man sie ohne Docker-Befehle nachlesen kann
try:
    CONFIG.mkdir(parents=True, exist_ok=True)
    _log_file = logging.handlers.RotatingFileHandler(CONFIG / "web.log", maxBytes=300_000, backupCount=1, encoding="utf-8")
    _log_file.setLevel(logging.WARNING)
    _log_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    app.logger.addHandler(_log_file)
except OSError:
    pass


# Meldungen entstehen im Code auf Deutsch; fuer die englische Oberflaeche werden sie hier uebersetzt.
# {0}, {1} ... stehen fuer eingesetzte Werte.
MESSAGES = [
    ("Im Archiv wird gerade schon etwas gespeichert. Bitte kurz warten.",
     "Something is already being saved in the archive. Please wait a moment."),
    ("'{0}' gibt es im Archiv schon. Es wurde nichts geändert.", "'{0}' already exists in the archive. Nothing was changed."),
    ("'{0}' liegt im Archiv als einzelne CD. Bitte dort zuerst die CD-Nummer eintragen.",
     "'{0}' is a single CD in the archive. Please enter its CD number there first."),
    ("Die Prüfsumme von '{0}' stimmt nach dem Schreiben nicht. Im Archiv wurde nichts geändert.",
     "The checksum of '{0}' does not match after writing. Nothing was changed in the archive."),
    ("Die neue Fassung liegt im Archiv, aber das Ersetzen der alten Dateien ist mittendrin gescheitert ({0}). "
     "Bitte „Archiv neu einlesen“ und den Ordner prüfen.",
     "The new version is in the archive, but replacing the old files failed halfway ({0}). "
     "Please use “Re-read archive” and check the folder."),
    ("'{0}' gibt es im Regal schon. Es wurde nichts verschoben.", "'{0}' already exists on the shelf. Nothing was moved."),
    ("Das Album liegt wieder im Regal, ließ sich im Archiv aber nicht löschen ({0}). Bitte dort von Hand löschen.",
     "The album is back on the shelf but could not be deleted in the archive ({0}). Please delete it there by hand."),
    ("Die Prüfsumme von '{0}' stimmt nach dem Kopieren nicht.", "The checksum of '{0}' does not match after copying."),
    ("Unbekanntes Format.", "Unknown format."),
    ("Dieses Album wird gerade ins Archiv verschoben.", "This album is being moved to the archive right now."),
    ("Das Album ließ sich nicht vollständig löschen. Vermutlich hat ein anderes Programm den Ordner oder eine Datei geöffnet.",
     "The album could not be deleted completely. Another program probably has the folder or a file open."),
    ("Der Ripper kann noch kein MP3. Bitte update.bat im cdripper-Ordner ausführen.",
     "The ripper cannot create MP3 yet. Please run update.bat in the cdripper folder."),
    ("Kein Album angegeben.", "No album given."),
    ("Ungültiger Pfad.", "Invalid path."),
    ("Album nicht gefunden.", "Album not found."),
    ("In diesem Ordner liegen keine Musikdateien.", "There are no music files in this folder."),
    ("'{0}' existiert bereits.", "'{0}' already exists."),
    ("Die Tags sind gespeichert, aber der Ordner lässt sich gerade nicht verschieben, weil ein anderes Programm darauf "
     "zugreift (zum Beispiel ein Explorer-Fenster oder ein Player). Schließ es und speichere noch einmal.",
     "The tags are saved, but the folder cannot be moved right now because another program is using it "
     "(for example an Explorer window or a player). Close it and save again."),
    ("Das ist keine gültige Bild-Adresse.", "That is not a valid image address."),
    ("Die Bild-Adresse lässt sich nicht auflösen.", "The image address cannot be resolved."),
    ("Bilder lassen sich nur von öffentlichen Web-Adressen laden.", "Images can only be loaded from public web addresses."),
    ("Cover können nur von Discogs geladen werden.", "Covers can only be loaded from Discogs."),
    ("Das Bild konnte nicht geladen werden.", "The image could not be loaded."),
    ("Unter dieser Adresse liegt kein Bild.", "There is no image at this address."),
    ("Das Bild ist zu groß.", "The image is too large."),
    ("Dieses Bildformat wird nicht unterstützt. Bitte ein JPEG oder PNG wählen.",
     "This image format is not supported. Please choose a JPEG or PNG."),
    ("Die Datei ist kein lesbares Bild.", "The file is not a readable image."),
    ("Das Cover muss ein JPEG- oder PNG-Bild sein.", "The cover must be a JPEG or PNG image."),
    ("Das Cover konnte nicht gelesen werden.", "The cover could not be read."),
    ("Das Cover ist zu groß (maximal 8 MB).", "The cover is too large (8 MB at most)."),
    ("Diese CD wird gerade gerippt. Bitte warten, bis der Rip fertig ist.",
     "This CD is being ripped right now. Please wait until the rip has finished."),
    ("Interpret und Album dürfen nicht leer sein.", "Artist and album must not be empty."),
    ("Die Titelliste passt nicht mehr zu den Dateien. Bitte neu laden.",
     "The track list no longer matches the files. Please reload."),
    ("Titelnummer fehlt bei '{0}'.", "Track number missing for '{0}'."),
    ("Eine Titelnummer ist auf CD{0} doppelt vergeben.", "A track number is used twice on CD{0}."),
    ("Eine Titelnummer ist doppelt vergeben.", "A track number is used twice."),
    ("Zwei Titel auf CD{0} würden denselben Dateinamen bekommen. Bitte Nummern und Titel prüfen.",
     "Two tracks on CD{0} would get the same file name. Please check numbers and titles."),
    ("Zwei Titel würden denselben Dateinamen bekommen. Bitte Nummern und Titel prüfen.",
     "Two tracks would get the same file name. Please check numbers and titles."),
    ("CD {0} von '{1}' gibt es bereits im Regal.", "CD {0} of '{1}' is already on the shelf."),
    ("'{0}' steht bereits als einzelne CD im Regal. Öffne dieses Album und trag dort zuerst seine CD-Nummer ein "
     "(zum Beispiel CD 1 von 2).",
     "'{0}' is already on the shelf as a single CD. Open that album and enter its disc number there first "
     "(for example CD 1 of 2)."),
    ("'{0}' ist bereits ein Album mit mehreren CDs. Trag bei „CD“ und „von“ ein, welche CD das hier ist, dann wird "
     "sie dort einsortiert.",
     "'{0}' already is an album with several CDs. Enter under “CD” and “of” which disc this is, and it will be "
     "filed there."),
    ("Der Ordner '{0}' existiert bereits.", "The folder '{0}' already exists."),
    ("Es ist noch kein Discogs-Token hinterlegt.", "No Discogs token has been stored yet."),
    ("Discogs ist gerade nicht erreichbar.", "Discogs cannot be reached right now."),
    ("Discogs lehnt das Token ab. Bitte in den Einstellungen prüfen.", "Discogs rejects the token. Please check it in the settings."),
    ("Bei Discogs nicht gefunden.", "Not found on Discogs."),
    ("Zu viele Anfragen an Discogs. Bitte eine Minute warten.", "Too many requests to Discogs. Please wait a minute."),
    ("Discogs antwortet mit Fehler {0}.", "Discogs answers with error {0}."),
    ("Discogs lehnt dieses Token ab. Es wurde nicht gespeichert.", "Discogs rejects this token. It was not saved."),
    ("Das Token konnte nicht geprüft werden und wurde nicht gespeichert. {0}",
     "The token could not be checked and was not saved. {0}"),
    ("Gerade wird keine CD ausgelesen.", "No CD is being read right now."),
    ("Der Ripper kann noch nicht abbrechen. Bitte update.bat im cdripper-Ordner ausführen.",
     "The ripper cannot cancel yet. Please run update.bat in the cdripper folder."),
    ("Eine CD kann nicht zu sich selbst hinzugefügt werden.", "A CD cannot be added to itself."),
    ("Nur einzelne CDs lassen sich zu einem Album hinzufügen.", "Only single CDs can be added to an album."),
    ("Eine der beiden CDs wird gerade gerippt. Bitte warten, bis der Rip fertig ist.",
     "One of the two CDs is being ripped right now. Please wait until the rip has finished."),
    ("Das Zielalbum ist selbst noch nicht erkannt. Trag dort zuerst Interpret und Album ein.",
     "The target album has not been identified itself. Enter artist and album there first."),
    ("Titelzahl und Titellängen passen zu CD {0} der Discogs-Ausgabe.",
     "Track count and track lengths match CD {0} of the Discogs release."),
    ("Die CD ist in ihren Tags bereits als CD {0} gekennzeichnet.", "The CD is already marked as CD {0} in its tags."),
    ("Die Titelzahl passt zu CD {0} der Discogs-Ausgabe.", "The track count matches CD {0} of the Discogs release."),
    ("CD {0} fehlt in diesem Album noch.", "CD {0} is still missing from this album."),
    ("Das ist die nächste freie Nummer.", "This is the next free number."),
    ("Bitte eine CD-Nummer zwischen 1 und 99 wählen.", "Please choose a disc number between 1 and 99."),
    ("CD {0} gibt es in diesem Album schon.", "CD {0} already exists in this album."),
    ("Titel nicht gefunden.", "Track not found."),
    ("Bitte einen Suchbegriff eingeben.", "Please enter a search term."),
    ("Unbekannte Sprache.", "Unknown language."),
    ("Der Archivordner muss ein vollständiger Windows-Pfad sein, zum Beispiel E:\\Musikarchiv.",
     "The archive folder must be a full Windows path, for example E:\\MusicArchive."),
    ("Der Archivordner ist im Container nicht eingebunden. Bitte update.bat im cdripper-Ordner ausführen.",
     "The archive folder is not mounted in the container. Please run update.bat in the cdripper folder."),
    ("'{0}' gibt es im Archiv schon. Es wurde nichts verschoben.", "'{0}' already exists in the archive. Nothing was moved."),
    ("Die Prüfsumme von '{0}' stimmt nach dem Kopieren nicht. Es wurde nichts verschoben, das Album liegt unverändert im Regal.",
     "The checksum of '{0}' does not match after copying. Nothing was moved; the album is unchanged on the shelf."),
    ("Das Kopieren ins Archiv ist fehlgeschlagen ({0}). Es wurde nichts verschoben, das Album liegt unverändert im Regal.",
     "Copying to the archive failed ({0}). Nothing was moved; the album is unchanged on the shelf."),
    ("Das Album liegt vollständig und geprüft im Archiv, aber das Original im Regal ließ sich nicht löschen ({0}). "
     "Bitte den Ordner von Hand löschen.",
     "The album is complete and verified in the archive, but the original on the shelf could not be deleted ({0}). "
     "Please delete the folder by hand."),
    ("Bitte zuerst die ungespeicherten Änderungen speichern.", "Please save the unsaved changes first."),
    ("Eine Netzwerkfreigabe muss die Form \\\\server\\freigabe oder \\\\server\\freigabe\\ordner haben.",
     "A network share must have the form \\\\server\\share or \\\\server\\share\\folder."),
    ("Im Container fehlt die Unterstützung für Netzwerkfreigaben. Bitte update.bat im cdripper-Ordner ausführen.",
     "The container lacks support for network shares. Please run update.bat in the cdripper folder."),
    ("Die Freigabe lehnt die Anmeldung ab. Bitte Benutzer und Kennwort prüfen.",
     "The share rejects the login. Please check user and password."),
    ("Der Server ist erreichbar, aber diese Freigabe gibt es dort nicht.",
     "The server can be reached, but this share does not exist there."),
    ("Der Archivordner ist nicht beschreibbar. Bitte die Berechtigungen prüfen.",
     "The archive folder is not writable. Please check the permissions."),
    ("Der Server der Freigabe ist nicht erreichbar. Bitte Namen oder IP-Adresse prüfen.",
     "The server of the share cannot be reached. Please check the name or IP address."),
    ("Der Zugriff auf das Archiv ist fehlgeschlagen ({0}).", "Access to the archive failed ({0})."),
    ("Der Container findet den Server '{0}' nicht unter seinem Namen. Trag statt des Namens seine IP-Adresse ein, "
     "zum Beispiel \\\\192.168.178.20\\freigabe. In der Windows-Eingabeaufforderung zeigt „ping {1}“ die Adresse an.",
     "The container cannot find the server '{0}' by its name. Enter its IP address instead of the name, for example "
     "\\\\192.168.178.20\\share. In the Windows command prompt, “ping {1}” shows the address."),
    ("Dieser Ordner ist noch nicht eingebunden und lässt sich deshalb noch nicht prüfen. Speichere die Einstellungen "
     "und führe update.bat im cdripper-Ordner aus.",
     "This folder is not mounted yet and therefore cannot be checked yet. Save the settings and run update.bat in "
     "the cdripper folder."),
]
_MESSAGE_RULES = [
    (re.compile(re.sub(r"\\\{(\d)\\\}", "(.+?)", re.escape(de)), re.S), en) for de, en in MESSAGES
]


def language():
    return "en" if load_settings().get("language") == "en" else "de"


def translate(message):
    """Deutsche Meldung in der eingestellten Sprache liefern."""
    if language() != "en":
        return message
    for pattern, english in _MESSAGE_RULES:
        match = pattern.fullmatch(message)
        if match:
            return english.format(*(translate(group) for group in match.groups()))
    return message


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({"error": translate(err.message)}), err.status


@app.errorhandler(Exception)
def handle_unexpected(err):
    if isinstance(err, HTTPException):
        return jsonify({"error": err.description}), err.code
    app.logger.exception("Unerwarteter Fehler")
    return jsonify({"error": f"Interner Fehler: {err}"}), 500


# ---------------------------------------------------------------- Einstellungen

def load_settings():
    try:
        return json.loads((CONFIG / "settings.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "settings.json").write_text(json.dumps(settings, indent=2), "utf-8")


def discogs_token():
    return (load_settings().get("discogs_token") or os.environ.get("DISCOGS_TOKEN") or "").strip()


def rip_format():
    try:
        value = FORMAT_FILE.read_text("utf-8").strip()
    except OSError:
        value = ""
    return value if value in FORMATS else "flac"


def mp3_supported():
    """Der Ripper meldet in seiner Statusdatei, ob er MP3 erzeugen kann."""
    return bool(ripper_status().get("mp3"))


def set_rip_format(value):
    if value not in FORMATS:
        raise ApiError("Unbekanntes Format.")
    if value == "mp3" and not mp3_supported():
        raise ApiError("Der Ripper kann noch kein MP3. Bitte update.bat im cdripper-Ordner ausführen.")
    FORMAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    FORMAT_FILE.write_text(value + "\n", "utf-8")


def year_prefix_on():
    try:
        return YEAR_FILE.read_text("utf-8").strip() == "1"
    except OSError:
        return False


def set_year_prefix(on):
    YEAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    YEAR_FILE.write_text("1\n" if on else "0\n", "utf-8")


def album_folder_name(album, year):
    """Name des Albumordners; je nach Einstellung mit vorangestelltem Jahr ("1992 - Into The Web")."""
    base = safe_name(album, "Unbekanntes Album")
    year = str(year or "").strip()[:4]
    return f"{year} - {base}" if year_prefix_on() and re.match(r"^\d{4}$", year) else base


UNC_PATH = re.compile(r'^\\\\([^\\/<>:"|?*%\x00-\x1f]+)\\([^\\/<>:"|?*%\x00-\x1f]+)((?:\\[^\\/<>:"|?*%\x00-\x1f]+)*)$')


def clean_archive_path(value):
    """Eingabe vereinheitlichen: Windows-Pfad (E:\\Archiv) oder Netzwerkfreigabe (\\\\server\\freigabe\\ordner)."""
    value = str(value or "").strip().strip('"')
    if value.lower().startswith("smb://"):
        value = "\\\\" + value[6:]
    value = value.replace("/", "\\").rstrip("\\ ")
    if not value:
        return "", ""
    if value.startswith("\\\\"):
        if not UNC_PATH.match(value):
            raise ApiError("Eine Netzwerkfreigabe muss die Form \\\\server\\freigabe oder \\\\server\\freigabe\\ordner haben.")
        return "smb", value
    if len(value) == 2:
        value += "\\"  # nur ein Laufwerk, z. B. "E:"
    if not re.match(r'^[A-Za-z]:\\[^<>:"|?*%\x00-\x1f]*$', value):
        raise ApiError("Der Archivordner muss ein vollständiger Windows-Pfad sein, zum Beispiel E:\\Musikarchiv.")
    return "local", value


def archive_settings():
    """Gespeichertes Archivziel: {'kind': 'local'|'smb'|'', 'path', 'user', 'password'}."""
    stored = load_settings().get("archive") or {}
    if stored.get("kind") == "smb":
        return {"kind": "smb", "path": stored.get("path", ""), "user": stored.get("user", ""), "password": stored.get("password", "")}
    try:
        local = ARCHIVE_WANTED_FILE.read_text("utf-8").strip()
    except OSError:
        local = ""
    return {"kind": "local" if local else "", "path": local, "user": "", "password": ""}


def set_archive(path, user=None, password=None):
    """Archivziel speichern. Ein lokaler Ordner wird beim naechsten Start ueber die .bat-Dateien eingebunden,
    eine Netzwerkfreigabe spricht die Weboberflaeche direkt an (kein Neustart noetig)."""
    kind, path = clean_archive_path(path)
    before = archive_settings()
    settings = load_settings()
    if kind == "smb":
        user = before["user"] if user is None else str(user).strip()
        same_target = before["kind"] == "smb" and before["path"].lower() == path.lower() and before["user"] == user
        # leeres Kennwortfeld = bisheriges Kennwort behalten, solange Freigabe und Benutzer gleich bleiben
        password = (before["password"] if same_target else "") if not password else str(password)
        settings["archive"] = {"kind": "smb", "path": path, "user": user, "password": password}
    else:
        settings.pop("archive", None)
    save_settings(settings)
    CONFIG.mkdir(parents=True, exist_ok=True)
    # die .bat-Dateien lesen diese Datei; bei einer Netzwerkfreigabe bleibt sie leer
    ARCHIVE_WANTED_FILE.write_bytes((path + "\r\n").encode("utf-8") if kind == "local" else b"")


def archive_view():
    target = archive_settings()
    if target["kind"] == "smb":
        return {"kind": "smb", "path": target["path"], "user": target["user"], "password_set": bool(target["password"]),
                "active": "", "pending": False, "available": smbclient is not None}
    same = target["path"].rstrip("\\").lower() == ARCHIVE_ACTIVE.rstrip("\\").lower()
    return {"kind": "local", "path": target["path"], "user": "", "password_set": False,
            "active": ARCHIVE_ACTIVE, "pending": not same, "available": ARCHIVE.is_dir()}


def archive_verify_on():
    """Kopien im Archiv per Pruefsumme gegen das Original pruefen (Standard: ja)."""
    return load_settings().get("archive_verify", True) is not False


def archive_auto_on():
    """Erkannte, vollstaendig gerippte CDs nach dem Rip von selbst ins Archiv verschieben (Standard: nein)."""
    return load_settings().get("archive_auto") is True


def settings_view():
    return {
        "archive_verify": archive_verify_on(),
        "archive_auto": archive_auto_on(),
        "language": language(),
        "archive": archive_view(),
        "year_prefix": year_prefix_on(),
        "discogs_token_set": bool(discogs_token()),
        "format": rip_format(),
        "mp3_available": mp3_supported(),
    }


# ---------------------------------------------------------------- Dateisystem

def first(tags, key, default=""):
    values = tags.get(key)
    return values[0] if values else default


def album_dir(rel):
    """Relativen Albumpfad sicher unterhalb von OUTPUT aufloesen."""
    if not rel:
        raise ApiError("Kein Album angegeben.")
    path = (OUTPUT / rel).resolve()
    if path != OUTPUT and OUTPUT not in path.parents:
        raise ApiError("Ungültiger Pfad.")
    if not path.is_dir():
        raise ApiError("Album nicht gefunden.", 404)
    return path


def audio_files(directory):
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
         and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


ID3_TEXT = {"title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "date": "TDRC", "label": "TPUB"}


def read_audio(path):
    """Tags als {name: [werte]} und Laenge in Sekunden - fuer FLAC und MP3 gleich aufgebaut."""
    if path.suffix.lower() == ".mp3":
        audio = MP3(path)
        id3 = audio.tags or {}
        tags = {key: [str(t) for t in id3[frame].text] for key, frame in ID3_TEXT.items() if frame in id3}
        if "TCON" in id3:
            tags["genre"] = list(id3["TCON"].genres)
        for frame, number, total in (("TRCK", "tracknumber", "tracktotal"), ("TPOS", "discnumber", "disctotal")):
            if frame in id3:
                parts = str(id3[frame].text[0]).split("/")
                tags[number] = [parts[0]]
                if len(parts) > 1:
                    tags[total] = [parts[1]]
        for frame in (id3.getall("TXXX") if id3 else []):
            tags[frame.desc.lower()] = [str(t) for t in frame.text]
    else:
        audio = FLAC(path)
        tags = audio.tags.as_dict() if audio.tags else {}
    return tags, round(audio.info.length) if audio.info else 0


def rel_id(directory, root=None):
    return directory.relative_to(root or OUTPUT).as_posix()


def ripper_status():
    try:
        return json.loads(STATUS_FILE.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def is_unknown(artist, album, directory):
    return (
        album.lower().startswith("unknown album")
        or directory.name.lower().startswith("unknown album")
        or (artist.lower() == "unknown artist" and not album)
    )


def to_int(value):
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


DISC_DIR = re.compile(r"^CD ?(\d+)$", re.I)
LEGACY_DISC = re.compile(r"^(.+?)\s*[(\[]\s*(?:CD|Disc|Disk)\s*(\d+)\s*[)\]]$", re.I)


def album_discs(directory):
    """Die CDs eines Albums als [(nummer, ordner, dateien)].

    Einzelne CD: die Titel liegen direkt im Albumordner, nummer ist None.
    Album mit mehreren CDs: die Titel liegen in den Unterordnern CD1, CD2, ...
    """
    files = audio_files(directory)
    if files:
        return [(None, directory, files)]
    discs = []
    for sub in directory.iterdir():
        match = DISC_DIR.match(sub.name) if sub.is_dir() else None
        if match:
            sub_files = audio_files(sub)
            if sub_files:
                discs.append((int(match.group(1)), sub, sub_files))
    return sorted(discs, key=lambda d: (d[0], d[1].name))


def disc_state(files, status):
    try:
        tags = read_audio(files[0])[0]
    except Exception:
        tags = {}
    cddb = first(tags, "cddb")
    newest = max(f.stat().st_mtime for f in files)
    total = to_int(first(tags, "tracktotal"))
    if status.get("state") in ACTIVE_STATES and status.get("disc_id"):
        busy = bool(cddb) and cddb == status["disc_id"]
    elif status:
        busy = False
    else:
        # Aelterer Ripper ohne Statusdatei: es fehlen noch Titel und es wurde gerade erst geschrieben
        busy = bool(total and total > len(files)) and time.time() - newest < BUSY_SECONDS
    return {"tags": tags, "cddb": cddb, "newest": newest, "total": total, "busy": busy,
            "incomplete": bool(total and total > len(files) and not busy)}


def album_summary(directory, discs, status, root=None):
    states = [disc_state(files, status) for _number, _dir, files in discs]
    multi = discs[0][0] is not None
    tags = states[0]["tags"]
    artist = first(tags, "albumartist") or first(tags, "artist")
    album = first(tags, "album")
    all_files = [f for _number, _dir, files in discs for f in files]
    return {
        "path": rel_id(directory, root),
        "artist": artist,
        "album": album,
        "year": first(tags, "date")[:4],
        "tracks": len(all_files),
        "track_total": None if multi else states[0]["total"],
        "unknown": is_unknown(artist, album, directory),
        "busy": any(s["busy"] for s in states),
        "incomplete": any(s["incomplete"] for s in states),
        "disc_id": states[0]["cddb"],
        "modified": max(s["newest"] for s in states),
        "format": "/".join(sorted({f.suffix.lower()[1:].upper() for f in all_files})),
        "multi": multi,
        "discs": len(discs) if multi else 0,
    }


def retag_disc(path, number):
    """Beim Zusammenfuehren: '(CD 2)' aus dem Albumtitel nehmen und die CD-Nummer eintragen."""
    def strip(album):
        match = LEGACY_DISC.match(album or "")
        return match.group(1).strip() if match else album

    if path.suffix.lower() == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        album = strip(str(tags["TALB"].text[0])) if "TALB" in tags else ""
        total = str(tags["TPOS"].text[0]).partition("/")[2] if "TPOS" in tags else ""
        tags.delall("TALB")
        tags.delall("TPOS")
        if album:
            tags.add(TALB(encoding=3, text=[album]))
        tags.add(TPOS(encoding=3, text=[f"{number}/{total}" if total else str(number)]))
        tags.save(path, v2_version=3)
    else:
        audio = FLAC(path)
        if audio.get("album"):
            audio["album"] = strip(audio["album"][0])
        audio["discnumber"] = str(number)
        audio.save()


def fix_trailing(directory):
    """Ordner, deren Name mit Punkt oder Leerzeichen endet, kann Windows nicht oeffnen: Endung abschneiden."""
    clean = directory.name.rstrip(". ")
    if clean == directory.name or not clean:
        return directory
    target = directory.with_name(clean)
    try:
        if not target.exists():
            directory.rename(target)
            app.logger.warning("Ordner umbenannt (Name endete mit Punkt/Leerzeichen): %s", rel_id(target))
            return target
    except OSError:
        app.logger.exception("Umbenennen von %s fehlgeschlagen", directory)
    return directory


def merge_legacy_discs():
    """Ordner wie 'Album (CD 2)' in die Struktur 'Album/CD2' ueberfuehren (einmalig je Ordner)."""
    for artist_dir in OUTPUT.iterdir():
        if not artist_dir.is_dir() or artist_dir.name.startswith("."):
            continue
        artist_dir = fix_trailing(artist_dir)
        for sub in list(artist_dir.iterdir()):
            if sub.is_dir():
                sub = fix_trailing(sub)
            match = LEGACY_DISC.match(sub.name) if sub.is_dir() else None
            files = audio_files(sub) if match else []
            if not files:
                continue
            container = artist_dir / safe_name(match.group(1), "Album")
            target = container / f"CD{int(match.group(2))}"
            if target.exists() or (container.is_dir() and audio_files(container)):
                continue  # Ziel belegt: lieber nichts anfassen
            try:
                for path in files:
                    retag_disc(path, int(match.group(2)))
                container.mkdir(exist_ok=True)
                sub.rename(target)
                app.logger.info("Zusammengefuehrt: %s -> %s", rel_id(sub), rel_id(target))
            except Exception:
                app.logger.exception("Zusammenfuehren von %s fehlgeschlagen", sub)


_rename_failed = {}  # Ordner, die sich nicht umbenennen liessen -> Zeitpunkt (nicht bei jedem Durchlauf neu versuchen)


def apply_year_scheme():
    """Albumordner an die Einstellung "Jahr voranstellen" angleichen: "Album" <-> "1992 - Album"."""
    on = year_prefix_on()
    for artist_dir in OUTPUT.iterdir():
        if not artist_dir.is_dir() or artist_dir.name.startswith("."):
            continue
        for sub in list(artist_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            match = YEAR_PREFIX.match(sub.name)
            if on == bool(match) or time.time() - _rename_failed.get(str(sub), 0) < 600:
                continue  # passt schon
            discs = album_discs(sub)
            if not discs:
                continue
            try:
                year = first(read_audio(discs[0][2][0])[0], "date")[:4]
            except Exception:
                continue
            if not re.match(r"^\d{4}$", year):
                continue  # ohne Jahr in den Tags bleibt der Ordner, wie er ist
            if on:
                target = sub.with_name(f"{year} - {sub.name}")
            elif match.group(1) == year:  # nur ein Jahr entfernen, das wirklich das Albumjahr ist
                target = sub.with_name(match.group(2))
            else:
                continue
            try:
                if not target.exists():
                    sub.rename(target)
            except OSError as err:
                _rename_failed[str(sub)] = time.time()
                app.logger.warning("Albumordner nicht umbenennbar: %s (%r)", sub, err)


def scan_albums():
    status = ripper_status()
    if status.get("state") not in ACTIVE_STATES:  # waehrend eines Rips keine Ordner bewegen
        merge_legacy_discs()
        apply_year_scheme()
    albums = []
    for root, dirs, _files in os.walk(OUTPUT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        directory = Path(root)
        if directory == OUTPUT:
            continue
        discs = album_discs(directory)
        if discs:
            try:
                albums.append(album_summary(directory, discs, status))
            except OSError:  # der Ripper tauscht gerade Dateien aus: beim naechsten Durchlauf wieder da
                continue
            if discs[0][0] is not None:  # CD-Unterordner gehoeren zu diesem Album
                dirs[:] = [d for d in dirs if not DISC_DIR.match(d)]
    # Nicht erkannte zuerst, dann nach Interpret und Album
    albums.sort(key=lambda a: (not a["unknown"], a["artist"].lower(), a["album"].lower(), a["path"]))
    return albums


def album_detail(directory):
    discs = album_discs(directory)
    if not discs:
        raise ApiError("In diesem Ordner liegen keine Musikdateien.", 404)
    summary = album_summary(directory, discs, ripper_status())
    tracks = []
    head = None
    for disc_number, _disc_dir, files in discs:
        for index, path in enumerate(files, start=1):
            tags, length = read_audio(path)
            if head is None:
                head = tags
            tracks.append({
                "file": path.relative_to(directory).as_posix(),
                "disc": disc_number,
                "number": to_int(first(tags, "tracknumber")) or index,
                "title": first(tags, "title"),
                "artist": first(tags, "artist"),
                "length": length,
            })
    multi = summary["multi"]
    summary.update({
        "genre": "; ".join(head.get("genre", [])),
        "discnumber": "" if multi else to_int(first(head, "discnumber")) or "",
        "disctotal": max(to_int(first(head, "disctotal")) or 0, discs[-1][0], len(discs)) if multi
        else to_int(first(head, "disctotal")) or "",
        "label": first(head, "label"),
        "catno": first(head, "catalognumber"),
        "discogs_id": first(head, "discogs_release_id"),
        "tracks": tracks,
    })
    return summary


INVALID_CHARS = re.compile(r'[:<>|*/"?\\\x00-\x1f]')


def safe_name(text, fallback):
    """Datei-/Ordnername, der auch unter Windows gueltig ist."""
    name = INVALID_CHARS.sub("", str(text or ""))
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").rstrip(". ")
    return name[:150].rstrip(". ") or fallback


def rename(src, dst):
    """Umbenennen, das auch reine Gross-/Kleinschreibungs-Aenderungen auf Windows-Laufwerken schafft."""
    if src == dst:
        return
    if dst.exists():
        if not src.samefile(dst):
            raise ApiError(f"'{dst.name}' existiert bereits.", 409)
        tmp = src.with_name(f".tmp-{uuid.uuid4().hex}")
        src.rename(tmp)
        tmp.rename(dst)
    else:
        src.rename(dst)


FOLDER_BUSY = ("Die Tags sind gespeichert, aber der Ordner lässt sich gerade nicht verschieben, weil ein anderes "
               "Programm darauf zugreift (zum Beispiel ein Explorer-Fenster oder ein Player). "
               "Schließ es und speichere noch einmal.")


def move_album(root, directory, target_dir):
    """Ordner innerhalb von root verschieben - auch in einen eigenen Unterordner (Album -> Album/CD1)."""
    if target_dir == directory:
        return
    old_parent = directory.parent
    holding = root / ".cdripper"
    holding.mkdir(exist_ok=True)
    tmp = holding / f"move-{uuid.uuid4().hex}"
    try:
        directory.rename(tmp)
    except OSError as err:
        app.logger.warning("Ordner nicht verschiebbar: %s (%r)", directory, err)
        raise ApiError(FOLDER_BUSY, 409)
    # Leer gewordene alte Ordner (Album-Container, Interpret) gleich entfernen. Aendert sich nur die
    # Gross-/Kleinschreibung, entstehen sie dadurch unten in der neuen Schreibweise neu.
    for parent in (old_parent, old_parent.parent):
        try:
            if root in parent.parents and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(target_dir)
    except OSError as err:
        app.logger.warning("Ordner nicht verschiebbar nach %s (%r)", target_dir, err)
        directory.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(directory)  # nichts verlieren: zurueck an den alten Platz
        try:  # eben angelegten, leer gebliebenen Zielordner wieder entfernen
            if target_dir.parent != directory.parent and not any(target_dir.parent.iterdir()):
                target_dir.parent.rmdir()
        except OSError:
            pass
        raise ApiError(FOLDER_BUSY, 409)
    # Windows-Laufwerke: Schreibweise der uebergeordneten Ordner angleichen. Das ist reine Kosmetik und
    # scheitert dort, solange noch etwas im Ordner geoeffnet ist - dann bleibt die alte Schreibweise stehen.
    for wanted in (target_dir.parent, target_dir.parent.parent):
        if root not in wanted.parents:
            continue
        try:
            actual = next((p for p in wanted.parent.iterdir() if p.name.lower() == wanted.name.lower()), None)
            if actual and actual.name != wanted.name and wanted.exists() and actual.samefile(wanted):
                rename(actual, wanted)
        except (OSError, ApiError) as err:
            app.logger.warning("Schreibweise von %s nicht angleichbar (%r)", wanted, err)


def real_path(path, root=None):
    """Den Pfad in der Schreibweise liefern, die tatsaechlich auf dem Laufwerk steht."""
    root = root or OUTPUT
    current = root
    try:
        for part in path.relative_to(root).parts:
            entries = list(current.iterdir())
            current = next((p for p in entries if p.name == part), None) \
                or next(p for p in entries if p.name.lower() == part.lower())
    except (OSError, StopIteration, ValueError):
        return path
    return current


def rename_staged(pairs):
    """Dateien ueber Zwischennamen umbenennen, damit Tausche nicht kollidieren."""
    staged = []
    for path, target in pairs:
        if path.name != target:
            tmp = path.with_name(f".tmp-{uuid.uuid4().hex}{path.suffix}")
            path.rename(tmp)
            staged.append((tmp, path.with_name(target)))
    for tmp, final in staged:
        tmp.rename(final)


def write_mp3_tags(path, meta, number, total, title, artist, cover):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    def put(frame, value):
        tags.delall(frame.__name__)
        values = [str(v) for v in (value if isinstance(value, list) else [value]) if str(v or "").strip()]
        if values:
            tags.add(frame(encoding=3, text=values))

    put(TIT2, title)
    put(TPE1, artist)
    put(TPE2, meta["albumartist"])
    put(TALB, meta["album"])
    put(TRCK, f"{number}/{total}")
    put(TDRC, meta["year"])
    put(TCON, "; ".join(meta["genres"]))  # ID3v2.3 kennt keine sauberen Mehrfachwerte
    put(TPOS, f"{meta['discnumber']}/{meta['disctotal']}" if meta["discnumber"] and meta["disctotal"]
        else meta["discnumber"] or "")
    put(TPUB, meta["label"])
    for desc, value in (("CATALOGNUMBER", meta["catno"]), ("DISCOGS_RELEASE_ID", meta["discogs_id"])):
        tags.delall(f"TXXX:{desc}")
        if value:
            tags.add(TXXX(encoding=3, desc=desc, text=[value]))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=cover[1], type=3, desc="Cover", data=cover[0]))
    tags.save(path, v2_version=3)  # ID3v2.3 versteht praktisch jedes Geraet


def set_tag(audio, key, value):
    if isinstance(value, list):
        value = [str(v).strip() for v in value if str(v).strip()]
    else:
        value = str(value).strip() if value not in (None, "") else ""
    if value:
        audio[key] = value
    elif key in audio:
        del audio[key]


MAX_COVER_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
COVER_EDGE = 1400  # groessere Bilder werden verkleinert, damit nicht mehrere MB in jeder Datei landen


def fetch_cover(url, any_host=False):
    """Bild von einer Web-Adresse laden. Ohne any_host nur von Discogs; sonst von jeder oeffentlichen Adresse."""
    parts = urlparse(str(url).strip())
    host = parts.hostname or ""
    if any_host:
        if parts.scheme not in ("http", "https") or not host:
            raise ApiError("Das ist keine gültige Bild-Adresse.")
        try:
            addresses = {info[4][0].split("%")[0] for info in socket.getaddrinfo(host, None)}
        except OSError:
            raise ApiError("Die Bild-Adresse lässt sich nicht auflösen.")
        if not all(ipaddress.ip_address(address).is_global for address in addresses):
            raise ApiError("Bilder lassen sich nur von öffentlichen Web-Adressen laden.")
    elif parts.scheme != "https" or not (host == "discogs.com" or host.endswith(".discogs.com")):
        raise ApiError("Cover können nur von Discogs geladen werden.")
    try:
        resp = requests.get(parts.geturl(), headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as err:
        app.logger.warning("Cover nicht ladbar von %s: %r", url, err)
        raise ApiError("Das Bild konnte nicht geladen werden.", 502)
    mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise ApiError("Unter dieser Adresse liegt kein Bild.", 502)
    if len(resp.content) > MAX_DOWNLOAD_BYTES:
        raise ApiError("Das Bild ist zu groß.")
    return resp.content, mime


def shrink_cover(cover):
    """Sehr grosse oder exotische Bilder (WebP usw.) in ein JPEG mit hoechstens COVER_EDGE Pixeln Kantenlaenge wandeln."""
    data, mime = cover
    if Image is None:
        if mime not in ("image/jpeg", "image/png"):
            raise ApiError("Dieses Bildformat wird nicht unterstützt. Bitte ein JPEG oder PNG wählen.")
        return data, mime, None
    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except Exception:
        raise ApiError("Die Datei ist kein lesbares Bild.")
    size = picture.size
    if mime == "image/jpeg" and max(size) <= COVER_EDGE and len(data) <= 1_500_000:
        return data, mime, size  # passt schon: nicht neu komprimieren
    if picture.mode in ("RGBA", "LA", "P"):
        picture = picture.convert("RGBA")
        canvas = Image.new("RGB", picture.size, (255, 255, 255))
        canvas.paste(picture, mask=picture.split()[-1])
        picture = canvas
    else:
        picture = picture.convert("RGB")
    picture.thumbnail((COVER_EDGE, COVER_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue(), "image/jpeg", picture.size

MAX_COVER_BYTES = 8 * 1024 * 1024


def decode_cover(data_url):
    """Hochgeladenes Cover (data:-URL aus dem Browser) pruefen und als (Bytes, Mime-Typ) liefern."""
    match = re.match(r"^data:image/(?:jpeg|png);base64,(.+)$", str(data_url), re.S)
    if not match:
        raise ApiError("Das Cover muss ein JPEG- oder PNG-Bild sein.")
    try:
        raw = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("Das Cover konnte nicht gelesen werden.")
    if len(raw) > MAX_COVER_BYTES:
        raise ApiError("Das Cover ist zu groß (maximal 8 MB).")
    if raw[:3] == b"\xff\xd8\xff":
        return raw, "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw, "image/png"
    raise ApiError("Das Cover muss ein JPEG- oder PNG-Bild sein.")


def write_cover_file(directory, cover):
    """Cover als cover.jpg bzw. cover.png neben die Titel legen; die jeweils andere Datei entfernen."""
    name, other = ("cover.png", "cover.jpg") if cover[1] == "image/png" else ("cover.jpg", "cover.png")
    (directory / name).write_bytes(cover[0])
    if (directory / other).is_file():
        (directory / other).unlink()


def same_dir(a, b):
    return a.exists() and b.exists() and a.samefile(b)


def save_album(directory, data, album_folder=None, root=None):
    """Tags schreiben und Dateien/Ordner benennen. root: Wurzel der Ablage (Regal, oder ein Arbeitsordner fuers Archiv)."""
    root = root or OUTPUT
    discs = album_discs(directory)
    if not discs:
        raise ApiError("In diesem Ordner liegen keine Musikdateien.", 404)
    multi = discs[0][0] is not None
    files = {p.relative_to(directory).as_posix(): (number, p) for number, _dir, fs in discs for p in fs}
    status = ripper_status() if root == OUTPUT else {"state": "idle"}
    if album_summary(directory, discs, status, root)["busy"]:
        raise ApiError("Diese CD wird gerade gerippt. Bitte warten, bis der Rip fertig ist.", 409)

    album = str(data.get("album") or "").strip()
    albumartist = str(data.get("artist") or "").strip()
    if not album or not albumartist:
        raise ApiError("Interpret und Album dürfen nicht leer sein.")

    tracks = data.get("tracks") or []
    if {t.get("file") for t in tracks} != set(files) or len(tracks) != len(files):
        raise ApiError("Die Titelliste passt nicht mehr zu den Dateien. Bitte neu laden.", 409)

    various = albumartist.lower() in ("various artists", "various")
    disctotal = to_int(data.get("disctotal"))
    if multi:
        discnumber = None
        disctotal = max(disctotal or 0, discs[-1][0], len(discs))
    else:
        discnumber = to_int(data.get("discnumber"))
    genres = [g for g in re.split(r"\s*;\s*", str(data.get("genre") or "")) if g]

    # Zielnamen vorab berechnen und je CD auf Dopplungen pruefen
    plan = []
    for track in tracks:
        disc, path = files[track["file"]]
        number = to_int(track.get("number"))
        if not number:
            raise ApiError(f"Titelnummer fehlt bei '{track.get('file')}'.")
        title = str(track.get("title") or "").strip() or f"Track {number}"
        artist = str(track.get("artist") or "").strip() or albumartist
        label = f"{artist} - {title}" if various else title
        target = f"{number:02d} - {safe_name(label, f'Track {number}')}{path.suffix.lower()}"
        plan.append((path, target, number, title, artist, disc))
    for disc in {p[5] for p in plan}:
        where = f" auf CD{disc}" if disc else ""
        numbers = [p[2] for p in plan if p[5] == disc]
        if len(set(numbers)) != len(numbers):
            raise ApiError(f"Eine Titelnummer ist{where} doppelt vergeben.")
        targets = [p[1].lower() for p in plan if p[5] == disc]
        if len(set(targets)) != len(targets):
            raise ApiError(f"Zwei Titel{where} würden denselben Dateinamen bekommen. Bitte Nummern und Titel prüfen.")

    # Zielordner: Interpret/Album, bei mehreren CDs Interpret/Album/CD1, CD2, ...
    if album_folder is None:  # beim Hinzufuegen zu einem bestehenden Album steht der Ordner schon fest
        album_folder = root / safe_name("Various Artists" if various else albumartist, "Unbekannt") \
            / album_folder_name(album, data.get("year"))
    into_set = not multi and bool(discnumber and disctotal and disctotal > 1)
    if into_set:
        target_dir = album_folder / f"CD{discnumber}"
        if target_dir.exists() and not same_dir(target_dir, directory):
            raise ApiError(f"CD {discnumber} von '{album}' gibt es bereits im Regal.", 409)
        if album_folder.is_dir() and not same_dir(album_folder, directory) and audio_files(album_folder):
            raise ApiError(f"'{album}' steht bereits als einzelne CD im Regal. Öffne dieses Album und trag dort "
                           "zuerst seine CD-Nummer ein (zum Beispiel CD 1 von 2).", 409)
    else:
        target_dir = album_folder
        if target_dir.exists() and not same_dir(target_dir, directory):
            other = album_discs(target_dir)
            if other and other[0][0] is not None and not multi:
                raise ApiError(f"'{album}' ist bereits ein Album mit mehreren CDs. Trag bei „CD“ und „von“ ein, "
                               "welche CD das hier ist, dann wird sie dort einsortiert.", 409)
            raise ApiError(f"Der Ordner '{rel_id(target_dir, root)}' existiert bereits.", 409)

    if data.get("cover_data"):  # eigenes Bild hat Vorrang vor dem Discogs-Cover
        cover = decode_cover(data["cover_data"])
    else:
        cover = fetch_cover(data["cover_url"]) if data.get("cover_url") else None
    meta = {
        "albumartist": "Various Artists" if various else albumartist, "album": album,
        "year": str(data.get("year") or "").strip(), "genres": genres,
        "discnumber": discnumber or "", "disctotal": disctotal or "",
        "label": str(data.get("label") or "").strip(),
        "catno": str(data.get("catno") or "").strip(),
        "discogs_id": str(data.get("discogs_id") or "").strip(),
    }

    # 1. Tags schreiben
    for path, _target, number, title, artist, disc in plan:
        total = sum(1 for p in plan if p[5] == disc)
        track_disc = disc or discnumber
        if path.suffix.lower() == ".mp3":
            write_mp3_tags(path, dict(meta, discnumber=track_disc or ""), number, total, title, artist, cover)
            continue
        audio = FLAC(path)
        set_tag(audio, "title", title)
        set_tag(audio, "artist", artist)
        set_tag(audio, "albumartist", meta["albumartist"])
        set_tag(audio, "album", album)
        set_tag(audio, "tracknumber", f"{number:02d}")
        set_tag(audio, "tracktotal", str(total))
        set_tag(audio, "date", meta["year"])
        set_tag(audio, "genre", genres)
        set_tag(audio, "discnumber", track_disc)
        set_tag(audio, "disctotal", disctotal)
        set_tag(audio, "label", meta["label"])
        set_tag(audio, "catalognumber", meta["catno"])
        set_tag(audio, "discogs_release_id", meta["discogs_id"])
        if cover:
            picture = Picture()
            picture.type = 3  # Front Cover
            picture.mime = cover[1]
            picture.data = cover[0]
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()

    # 2. Dateien umbenennen, CD-Ordner einheitlich benennen
    rename_staged([(p[0], p[1]) for p in plan])
    if cover:
        write_cover_file(directory, cover)
    if multi:
        for number, disc_dir, _files in discs:
            rename(disc_dir, directory / f"CD{number}")

    # 3. Ordner verschieben
    move_album(root, directory, target_dir)

    if into_set:  # Gesamtzahl der CDs im ganzen Album einheitlich halten
        album_parts = album_discs(album_folder)
        total = max([disctotal] + [number for number, _dir, _files in album_parts if number])
        for _number, _dir, part_files in album_parts:
            for path in part_files:
                set_disc_total(path, total)

    return rel_id(real_path(album_folder if into_set else target_dir, root), root)


# ---------------------------------------------------------------- Discogs

def discogs_get(path, params=None, token=None):
    token = token or discogs_token()
    if not token:
        raise ApiError("Es ist noch kein Discogs-Token hinterlegt.", 401)
    try:
        resp = requests.get(
            f"{DISCOGS_API}{path}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Authorization": f"Discogs token={token}"},
            timeout=20,
        )
    except requests.RequestException as err:
        app.logger.warning("Discogs nicht erreichbar: %s", err)
        raise ApiError("Discogs ist gerade nicht erreichbar.", 502)
    if resp.status_code == 401:
        raise ApiError("Discogs lehnt das Token ab. Bitte in den Einstellungen prüfen.", 401)
    if resp.status_code == 404:
        raise ApiError("Bei Discogs nicht gefunden.", 404)
    if resp.status_code == 429:
        raise ApiError("Zu viele Anfragen an Discogs. Bitte eine Minute warten.", 429)
    if not resp.ok:
        raise ApiError(f"Discogs antwortet mit Fehler {resp.status_code}.", 502)
    return resp.json()


def check_discogs_token(token):
    """Token bei Discogs pruefen; liefert den Benutzernamen des Kontos."""
    try:
        identity = discogs_get("/oauth/identity", token=token)
    except ApiError as err:
        if err.status == 401:
            raise ApiError("Discogs lehnt dieses Token ab. Es wurde nicht gespeichert.", 400)
        raise ApiError(f"Das Token konnte nicht geprüft werden und wurde nicht gespeichert. {err.message}", err.status)
    return identity.get("username") or ""


def clean_artist(name):
    name = re.sub(r"\s\(\d+\)$", "", (name or "").strip())
    match = re.match(r"^(.*), (The|Die|Der|Das|Les|Los|Las|La|Le|El)$", name)
    if match:
        name = f"{match.group(2)} {match.group(1)}"
    return "Various Artists" if name == "Various" else name


def join_artists(artists):
    out = ""
    for index, artist in enumerate(artists or []):
        out += clean_artist(artist.get("anv") or artist.get("name"))
        if index < len(artists) - 1:
            joiner = (artist.get("join") or ",").strip()
            out += ", " if joiner == "," else f" {joiner} "
    return out.strip()


MEDIA_PREFIXES = {"CD", "DVD", "BD", "SACD", "CDR", "DISC", "DISK", "D"}


def disc_key(position):
    pos = (position or "").strip()
    match = re.match(r"^([A-Za-z]*)\s*(\d+)\s*[-.:]\s*\d+", pos)
    if match:
        return f"{match.group(1).upper() or 'CD'} {int(match.group(2))}"
    match = re.match(r"^([A-Za-z]+)\s*-?\s*\d+$", pos)
    if match and match.group(1).upper() in MEDIA_PREFIXES - {"D"}:
        return match.group(1).upper()
    return ""


def flatten_tracklist(tracklist):
    for entry in tracklist or []:
        kind = entry.get("type_")
        subs = entry.get("sub_tracks") or []
        if kind == "heading":
            continue
        if kind == "index" and subs and all(
            re.match(r"^([A-Za-z]*\s*\d+\s*[-.:]\s*)?\d+$", (s.get("position") or "").strip()) for s in subs
        ):
            yield from subs  # eigenstaendige Titel unter einer Zwischenueberschrift
        else:
            yield entry


def normalize_release(data):
    artist = join_artists(data.get("artists"))
    discs, order = {}, []
    for entry in flatten_tracklist(data.get("tracklist")):
        key = disc_key(entry.get("position"))
        if key not in discs:
            discs[key] = []
            order.append(key)
        discs[key].append({
            "position": entry.get("position") or "",
            "title": entry.get("title") or "",
            "artist": join_artists(entry.get("artists")) or artist,
            "duration": entry.get("duration") or "",
        })
    images = data.get("images") or []
    primary = next((i for i in images if i.get("type") == "primary"), images[0] if images else {})
    label = (data.get("labels") or [{}])[0]
    return {
        "id": data.get("id"),
        "artist": artist,
        "album": data.get("title") or "",
        "year": str(data.get("year") or "") if data.get("year") else "",
        "genre": "; ".join(data.get("genres") or []),
        "styles": data.get("styles") or [],
        "label": re.sub(r"\s\(\d+\)$", "", label.get("name") or ""),
        "catno": "" if (label.get("catno") or "").lower() == "none" else label.get("catno") or "",
        "country": data.get("country") or "",
        "cover": primary.get("uri") or "",
        "url": data.get("uri") or "",
        "discs": [
            {"name": key or "CD", "number": index, "tracks": discs[key]}
            for index, key in enumerate(order, start=1)
        ],
    }


# ---------------------------------------------------------------- Routen

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.get("/api/albums")
def api_albums():
    return jsonify({"albums": scan_albums(), "ripper": ripper_status()})


@app.get("/api/rip")
def api_rip():
    """Stand des laufenden Rips (liest nur die kleine Statusdatei des Rippers)."""
    status = ripper_status()
    status["cancelling"] = status.get("state") == "ripping" and CANCEL_FILE.exists()
    # Ergebnis des letzten automatischen Archivierens, damit die Oberflaeche es einmal melden kann
    status["archive"] = dict(ARCHIVE_PROGRESS) or None
    status["auto_archive"] = dict(AUTO_EVENT, message=translate(AUTO_EVENT["message"]))
    return jsonify(status)


@app.post("/api/rip/cancel")
def api_rip_cancel():
    status = ripper_status()
    if status.get("state") != "ripping":
        raise ApiError("Gerade wird keine CD ausgelesen.", 409)
    if not status.get("cancel"):
        raise ApiError("Der Ripper kann noch nicht abbrechen. Bitte update.bat im cdripper-Ordner ausführen.", 409)
    CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.write_text(str(status.get("disc_id") or ""), "utf-8")
    return jsonify({"ok": True})


@app.get("/api/album")
def api_album():
    return jsonify(album_detail(album_dir(request.args.get("path"))))


@app.post("/api/album")
def api_album_save():
    directory = album_dir(request.args.get("path"))
    path = save_album(directory, request.get_json(force=True) or {})
    remember_discogs(path)
    return jsonify({"path": path})


def album_cover(directory):
    """Cover eines Albums als (Bytes, Mime-Typ) oder None: Bilddatei im Ordner, sonst das eingebettete Bild."""
    discs = album_discs(directory)
    for folder in [directory] + [disc_dir for _number, disc_dir, _files in discs]:
        for name in ("cover.jpg", "folder.jpg", "cover.png"):
            if (folder / name).is_file():
                return (folder / name).read_bytes(), "image/png" if name.endswith(".png") else "image/jpeg"
    files = discs[0][2] if discs else []
    if files and files[0].suffix.lower() == ".mp3":
        try:
            pictures = ID3(files[0]).getall("APIC")
        except ID3NoHeaderError:
            pictures = []
    else:
        pictures = FLAC(files[0]).pictures if files else []
    if pictures:
        return pictures[0].data, pictures[0].mime or "image/jpeg"
    return None


THUMBS = {}  # kleine Cover fuer die Albumliste: (Pfad, Stand) -> JPEG-Bytes


def cover_thumb(cover, edge):
    """Cover auf hoechstens edge Pixel verkleinern (fuer die Albumliste)."""
    if Image is None:
        return cover
    picture = Image.open(io.BytesIO(cover[0]))
    picture.draft("RGB", (edge, edge))  # JPEG gleich verkleinert dekodieren: schnell auch bei grossen Bildern
    picture = picture.convert("RGB")
    picture.thumbnail((edge, edge), Image.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=82)
    return out.getvalue(), "image/jpeg"


@app.get("/api/cover")
def api_cover():
    directory = album_dir(request.args.get("path"))
    edge = min(to_int(request.args.get("size")) or 0, 600)
    key = (rel_id(directory), request.args.get("t", ""), edge)
    if edge and key in THUMBS:
        cover = THUMBS[key]
    else:
        cover = album_cover(directory)
        if cover and edge:
            try:
                cover = cover_thumb(cover, edge)
            except Exception:
                cover = None
            if len(THUMBS) > 1000:
                THUMBS.clear()
            THUMBS[key] = cover
    if not cover:
        response = Response(status=404)
    else:
        response = Response(cover[0], mimetype=cover[1])
    # mit Stand in der Adresse darf der Browser das Bild behalten; sonst immer neu fragen
    response.headers["Cache-Control"] = "max-age=86400" if edge and request.args.get("t") else "no-cache"
    return response


# ---------------------------------------------------------------- CD zu einem Album hinzufuegen

def to_seconds(text):
    match = re.match(r"^(?:(\d+):)?(\d+):(\d{1,2})$", (text or "").strip())
    if not match:
        return None
    return int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + int(match.group(3))


def set_disc_total(path, total):
    if path.suffix.lower() == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return
        number = str(tags["TPOS"].text[0]).partition("/")[0] if "TPOS" in tags else ""
        if number:
            tags.delall("TPOS")
            tags.add(TPOS(encoding=3, text=[f"{number}/{total}"]))
            tags.save(path, v2_version=3)
    else:
        audio = FLAC(path)
        if audio.get("disctotal") != [str(total)]:
            audio["disctotal"] = str(total)
            audio.save()


def attach_plan(source_rel, target_rel):
    """Prueft, ob die CD source zum Album target hinzugefuegt werden kann, und schlaegt eine CD-Nummer vor."""
    source_dir, target_dir = album_dir(source_rel), album_dir(target_rel)
    if source_dir == target_dir:
        raise ApiError("Eine CD kann nicht zu sich selbst hinzugefügt werden.")
    source, target = album_detail(source_dir), album_detail(target_dir)
    if source["multi"]:
        raise ApiError("Nur einzelne CDs lassen sich zu einem Album hinzufügen.")
    if source["busy"] or target["busy"]:
        raise ApiError("Eine der beiden CDs wird gerade gerippt. Bitte warten, bis der Rip fertig ist.", 409)
    if target["unknown"]:
        raise ApiError("Das Zielalbum ist selbst noch nicht erkannt. Trag dort zuerst Interpret und Album ein.")

    if target["multi"]:
        taken = sorted({t["disc"] for t in target["tracks"]})
    else:  # das bisherige Einzelalbum wird zu CD1 (oder behaelt seine eingetragene Nummer)
        taken = [to_int(target["discnumber"]) or 1]

    release, exact, by_count = None, [], []
    if target["discogs_id"] and discogs_token():
        try:
            release = normalize_release(discogs_get(f"/releases/{to_int(target['discogs_id'])}"))
        except ApiError:
            release = None  # ohne Discogs geht es mit dem einfachen Vorschlag weiter
    if release and len(release["discs"]) > 1:
        lengths = [t["length"] for t in source["tracks"]]
        for disc in release["discs"]:
            if len(disc["tracks"]) != len(lengths):
                continue
            by_count.append(disc["number"])
            pairs = [(to_seconds(t["duration"]), length) for t, length in zip(disc["tracks"], lengths)]
            pairs = [(a, b) for a, b in pairs if a is not None]
            if pairs and all(abs(a - b) <= 4 for a, b in pairs):
                exact.append(disc["number"])

    free_exact = [n for n in exact if n not in taken]
    free_count = [n for n in by_count if n not in taken]
    if len(free_exact) == 1:
        suggestion, reason = free_exact[0], f"Titelzahl und Titellängen passen zu CD {free_exact[0]} der Discogs-Ausgabe."
    elif to_int(source["discnumber"]) and to_int(source["discnumber"]) not in taken:
        suggestion = to_int(source["discnumber"])
        reason = f"Die CD ist in ihren Tags bereits als CD {suggestion} gekennzeichnet."
    elif len(free_count) == 1:
        suggestion, reason = free_count[0], f"Die Titelzahl passt zu CD {free_count[0]} der Discogs-Ausgabe."
    else:
        suggestion = next(n for n in range(1, 1000) if n not in taken)
        reason = (f"CD {suggestion} fehlt in diesem Album noch." if suggestion < max(taken)
                  else "Das ist die nächste freie Nummer.")

    total = max(to_int(target["disctotal"]) or 0, len(release["discs"]) if release else 0, max(taken))
    return {
        "source": {"path": source["path"], "tracks": len(source["tracks"]), "unknown": source["unknown"],
                   "artist": source["artist"], "album": source["album"]},
        "target": {"path": target["path"], "artist": target["artist"], "album": target["album"], "multi": target["multi"]},
        "taken": taken,
        "total": total,
        "suggestion": suggestion,
        "reason": translate(reason),
        "discogs_discs": by_count,  # fuer diese Nummern koennen die Titelnamen von Discogs kommen
        "_source": source, "_target": target, "_release": release,
        "_source_dir": source_dir, "_target_dir": target_dir,
    }


def attach_disc(source_rel, target_rel, number, use_discogs):
    plan = attach_plan(source_rel, target_rel)
    source, target, release = plan["_source"], plan["_target"], plan["_release"]
    source_dir, target_dir = plan["_source_dir"], plan["_target_dir"]
    if not number or number < 1 or number > 99:
        raise ApiError("Bitte eine CD-Nummer zwischen 1 und 99 wählen.")
    if number in plan["taken"]:
        raise ApiError(f"CD {number} gibt es in diesem Album schon.", 409)
    total = max(plan["total"], number, len(plan["taken"]) + 1)
    cover = album_cover(target_dir)

    # Bisheriges Einzelalbum: seine Titel wandern zuerst in den Unterordner CD1
    if not target["multi"]:
        own = plan["taken"][0]
        for _n, _d, files in album_discs(target_dir):
            for path in files:
                retag_disc(path, own)
        move_album(OUTPUT, target_dir, target_dir / f"CD{own}")

    tracks = [{"file": t["file"], "number": t["number"], "title": t["title"],
               "artist": "" if t["artist"].lower() in ("", "unknown artist") else t["artist"]}
              for t in source["tracks"]]
    disc = next((d for d in release["discs"] if d["number"] == number), None) if release and use_discogs else None
    if disc and len(disc["tracks"]) == len(tracks):
        for index, (track, info) in enumerate(zip(tracks, disc["tracks"]), start=1):
            track.update(number=index, title=info["title"], artist=info["artist"])

    data = {
        "artist": target["artist"], "album": target["album"], "year": target["year"], "genre": target["genre"],
        "label": target["label"], "catno": target["catno"], "discogs_id": target["discogs_id"],
        "discnumber": number, "disctotal": total, "tracks": tracks,
    }
    if cover and cover[1] in ("image/jpeg", "image/png"):
        data["cover_data"] = f"data:{cover[1]};base64,{base64.b64encode(cover[0]).decode()}"
    save_album(source_dir, data, album_folder=target_dir)

    # Das Cover liegt schon im Albumordner; die Kopie im CD-Ordner ist ueberfluessig
    if any((target_dir / name).is_file() for name in ("cover.jpg", "cover.png")):
        for name in ("cover.jpg", "cover.png"):
            extra = target_dir / f"CD{number}" / name
            if extra.is_file():
                extra.unlink()

    # Gesamtzahl der CDs auf allen Titeln des Albums einheitlich eintragen
    for _n, _d, files in album_discs(target_dir):
        for path in files:
            set_disc_total(path, total)
    return rel_id(real_path(target_dir))


# ---------------------------------------------------------------- Archiv

class LocalStore:
    """Archiv in einem eingebundenen Ordner."""

    def __init__(self, root):
        self.root = Path(root)

    def check(self):
        if not self.root.is_dir():
            raise ApiError("Der Archivordner ist im Container nicht eingebunden. Bitte update.bat im cdripper-Ordner ausführen.")

    def _p(self, parts):
        return self.root.joinpath(*parts)

    def exists(self, parts):
        return self._p(parts).exists()

    def makedirs(self, parts):
        self._p(parts).mkdir(parents=True, exist_ok=True)

    def open(self, parts, mode):
        return open(self._p(parts), mode)

    def rename(self, old, new):
        self._p(old).rename(self._p(new))

    def rmtree(self, parts):
        shutil.rmtree(self._p(parts), ignore_errors=True)

    def remove(self, parts):
        self._p(parts).unlink()

    def rmdir_if_empty(self, parts):
        path = self._p(parts)
        if parts and path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    def scandir(self, parts):
        """[(name, ist_ordner, groesse, aenderungszeit)]"""
        result = []
        with os.scandir(self._p(parts)) as entries:
            for entry in entries:
                info = entry.stat()
                result.append((entry.name, entry.is_dir(), info.st_size, info.st_mtime))
        return result


# Namenszusaetze, unter denen Heimrouter die Geraete im Netz bekannt machen (FRITZ!Box: name.fritz.box)
LAN_SUFFIXES = (".fritz.box", ".lan", ".home", ".home.arpa", ".local", ".localdomain", ".speedport.ip")


def resolve_server(name):
    """Servernamen so liefern, dass der Container ihn findet.

    Windows findet Rechner im Heimnetz ueber ihren Kurznamen (NetBIOS), ein Container nur ueber DNS.
    Deshalb werden zusaetzlich die ueblichen Router-Zusaetze probiert.
    """
    candidates = [name] + ([name + suffix for suffix in LAN_SUFFIXES] if "." not in name else [])
    for candidate in candidates:
        try:
            socket.getaddrinfo(candidate, SMB_PORT)
            return candidate
        except OSError:
            continue
    raise ApiError(f"Der Container findet den Server '{name}' nicht unter seinem Namen. Trag statt des Namens seine "
                   f"IP-Adresse ein, zum Beispiel \\\\192.168.178.20\\freigabe. In der Windows-Eingabeaufforderung zeigt "
                   f"„ping {name}“ die Adresse an.")


class SmbStore:
    """Archiv auf einer Netzwerkfreigabe (\\\\server\\freigabe\\ordner), direkt per SMB angesprochen."""

    def __init__(self, unc, user, password):
        match = UNC_PATH.match(unc)
        self.unc, self.server = unc, match.group(1)
        self.share = f"\\\\{match.group(1)}\\{match.group(2)}"          # \\\\server\\freigabe
        self.subdirs = tuple(part for part in match.group(3).split("\\") if part)  # Ordner innerhalb der Freigabe
        self.user, self.password = user or "guest", password or ""
        self.options = {"port": SMB_PORT}

    def check(self):
        if smbclient is None:
            raise ApiError("Im Container fehlt die Unterstützung für Netzwerkfreigaben. Bitte update.bat im cdripper-Ordner ausführen.")
        host = resolve_server(self.server)
        if host != self.server:  # unter dem Kurznamen nicht auffindbar, aber z. B. als name.fritz.box
            self.unc = "\\\\" + host + self.unc[2 + len(self.server):]
            self.share = "\\\\" + host + self.share[2 + len(self.server):]
            self.server = host
        smbclient.reset_connection_cache()  # geaenderte Zugangsdaten sofort anwenden
        smbclient.register_session(self.server, username=self.user, password=self.password,
                                   port=SMB_PORT, connection_timeout=15)
        smbclient.listdir(self.share, **self.options)  # Freigabe vorhanden und lesbar?
        self.makedirs(())

    def _p(self, parts):
        return self.unc + "".join("\\" + part for part in parts)

    def _exists(self, path):
        try:
            return smbclient.path.exists(path, **self.options)
        except Exception as err:  # manche Server melden "nicht vorhanden" mit einem unueblichen Fehlercode
            if any(word in str(err).lower() for word in ("no_such_file", "not_found", "no such file")):
                return False
            raise

    def exists(self, parts):
        return self._exists(self._p(parts))

    def makedirs(self, parts):
        # Ebene fuer Ebene anlegen: verlaesst sich nicht darauf, wie der Server fehlende Zwischenordner meldet
        path = self.share
        for part in self.subdirs + tuple(parts):
            path += "\\" + part
            if not self._exists(path):
                smbclient.mkdir(path, **self.options)

    def open(self, parts, mode):
        return smbclient.open_file(self._p(parts), mode=mode, **self.options)

    def rename(self, old, new):
        smbclient.rename(self._p(old), self._p(new), **self.options)

    def rmtree(self, parts):
        try:
            for name in smbclient.listdir(self._p(parts), **self.options):
                child = tuple(parts) + (name,)
                if smbclient.path.isdir(self._p(child), **self.options):
                    self.rmtree(child)
                else:
                    smbclient.remove(self._p(child), **self.options)
            smbclient.rmdir(self._p(parts), **self.options)
        except Exception:
            app.logger.exception("Aufraeumen auf der Freigabe fehlgeschlagen: %s", self._p(parts))

    def remove(self, parts):
        smbclient.remove(self._p(parts), **self.options)

    def rmdir_if_empty(self, parts):
        if parts and not smbclient.listdir(self._p(parts), **self.options):
            smbclient.rmdir(self._p(parts), **self.options)

    def scandir(self, parts):
        """[(name, ist_ordner, groesse, aenderungszeit)]"""
        result = []
        for entry in smbclient.scandir(self._p(parts), **self.options):
            if entry.name in (".", ".."):
                continue
            info = getattr(entry, "smb_info", None)  # steht schon in der Verzeichnisantwort: keine Extra-Anfrage
            if info is not None:
                stamp = info.last_write_time
                result.append((entry.name, bool(info.file_attributes & 0x10), info.end_of_file,
                               stamp.timestamp() if hasattr(stamp, "timestamp") else float(stamp or 0)))
            else:
                stat = entry.stat()
                result.append((entry.name, entry.is_dir(), stat.st_size, stat.st_mtime))
        return result


def archive_store(target=None):
    target = target or archive_settings()
    if target["kind"] == "smb":
        return SmbStore(target["path"], target["user"], target["password"])
    return LocalStore(ARCHIVE)


def archive_error(err):
    """Fehler beim Zugriff auf das Archiv in eine verstaendliche Meldung uebersetzen."""
    text = f"{type(err).__name__} {err}".lower()
    if "guest" in text or "logon_failure" in text or "authentication" in text or "account" in text or "password" in text:
        return "Die Freigabe lehnt die Anmeldung ab. Bitte Benutzer und Kennwort prüfen."
    if "bad_network_name" in text or "network name" in text:
        return "Der Server ist erreichbar, aber diese Freigabe gibt es dort nicht."
    if "access_denied" in text or "permission" in text or "read-only" in text or "media_write_protected" in text:
        return "Der Archivordner ist nicht beschreibbar. Bitte die Berechtigungen prüfen."
    if any(word in text for word in ("timed out", "timeout", "refused", "unreachable", "name or service", "getaddrinfo",
                                     "failed to connect", "no route", "connection")):
        return "Der Server der Freigabe ist nicht erreichbar. Bitte Namen oder IP-Adresse prüfen."
    return f"Der Zugriff auf das Archiv ist fehlgeschlagen ({type(err).__name__})."


def test_archive(target):
    """Pruefen, ob sich im Archiv schreiben laesst: Testdatei anlegen, zuruecklesen, wieder loeschen."""
    store = archive_store(target)
    name = (f".cd-regal-schreibtest-{uuid.uuid4().hex}",)
    payload = uuid.uuid4().hex.encode()
    try:
        store.check()
        with store.open(name, "wb") as handle:
            handle.write(payload)
        with store.open(name, "rb") as handle:
            readback = handle.read()
        store.remove(name)
    except ApiError:
        raise
    except Exception as err:
        app.logger.warning("Archivtest fehlgeschlagen: %r", err)
        raise ApiError(archive_error(err))
    if readback != payload:
        raise ApiError("Der Archivordner ist nicht beschreibbar. Bitte die Berechtigungen prüfen.")


ARCHIVE_MARKER = ".cd-regal-unvollstaendig"  # liegt im Albumordner des Archivs, solange die Kopie nicht fertig geprueft ist


def discard_copy(store, target):
    """Unvollstaendige Kopie im Archiv wieder entfernen, samt eben angelegtem leeren Interpreten-Ordner."""
    try:
        store.rmtree(target)
        store.rmdir_if_empty(target[:-1])
    except Exception:
        app.logger.exception("Aufraeumen im Archiv fehlgeschlagen")


ARCHIVE_PROGRESS = {}  # Stand des gerade laufenden Verschiebens, fuer die Anzeige in der Oberflaeche


def archive_album(directory):
    """Album ins Archiv verschieben und dabei den Fortschritt fuer die Oberflaeche fuehren."""
    try:
        return _archive_album(directory)
    finally:
        ARCHIVE_PROGRESS.clear()


def _archive_album(directory):
    """Album samt Interpreten-/Albumordner ins Archiv verschieben.

    Erst kopieren, dann jede Kopie per SHA-256 gegen das Original pruefen, und erst wenn alles stimmt,
    das Original im Regal loeschen. Bis dahin bleibt das Regal unveraendert.
    """
    discs = album_discs(directory)
    if not discs:
        raise ApiError("In diesem Ordner liegen keine Musikdateien.", 404)
    if album_summary(directory, discs, ripper_status())["busy"]:
        raise ApiError("Diese CD wird gerade gerippt. Bitte warten, bis der Rip fertig ist.", 409)
    rel = directory.relative_to(OUTPUT)  # Interpret/Album
    target = tuple(rel.parts)
    marker = target + (ARCHIVE_MARKER,)
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    total = 0
    verify = archive_verify_on()
    store = archive_store()
    try:
        store.check()
        if store.exists(target):
            if not store.exists(marker):
                raise ApiError(f"'{rel.as_posix()}' gibt es im Archiv schon. Es wurde nichts verschoben.", 409)
            store.rmtree(target)  # Rest eines frueheren, abgebrochenen Versuchs
    except ApiError:
        raise
    except Exception as err:
        app.logger.warning("Archiv nicht erreichbar: %r", err)
        raise ApiError(archive_error(err))
    try:
        # Direkt in den endgueltigen Ordner kopieren; eine Markierungsdatei kennzeichnet ihn bis zum Schluss
        # als unvollstaendig. (Kein Umbenennen ganzer Ordner: das beherrscht nicht jeder Freigabe-Server.)
        store.makedirs(target)
        with store.open(marker, "wb") as handle:
            handle.write(b"Diese Kopie ist noch nicht vollstaendig geprueft.\n")
        work = sum(p.stat().st_size for p in files) * (2 if verify else 1) or 1  # Bytes: kopieren (+ zuruecklesen)
        done = 0

        def progress(index, phase):
            ARCHIVE_PROGRESS.update(path=rel.as_posix(), name=" - ".join(rel.parts), file=index, files=len(files),
                                    phase=phase, percent=min(100, done * 100 // work))

        for index, source in enumerate(files, 1):
            progress(index, "copy")
            inner = tuple(source.relative_to(directory).parts)
            store.makedirs(target + inner[:-1])
            digest = hashlib.sha256()
            with open(source, "rb") as reader, store.open(target + inner, "wb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(chunk)
                    writer.write(chunk)
                    total += len(chunk)
                    done += len(chunk)
                    progress(index, "copy")
                writer.flush()
                try:
                    os.fsync(writer.fileno())  # wirklich auf den Datentraeger schreiben, bevor zurueckgelesen wird
                except (OSError, AttributeError, ValueError):
                    pass
            if not verify:
                continue
            # Kopie aus dem Archiv zuruecklesen und mit dem Original vergleichen
            check = hashlib.sha256()
            progress(index, "verify")
            with store.open(target + inner, "rb") as reader:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    check.update(chunk)
                    done += len(chunk)
                    progress(index, "verify")
            if check.hexdigest() != digest.hexdigest():
                raise ApiError(f"Die Prüfsumme von '{'/'.join(inner)}' stimmt nach dem Kopieren "
                               "nicht. Es wurde nichts verschoben, das Album liegt unverändert im Regal.", 500)
        store.remove(marker)  # alles kopiert und geprueft
    except ApiError:
        discard_copy(store, target)
        raise
    except Exception as err:
        app.logger.exception("Archivieren von %s fehlgeschlagen", directory)
        discard_copy(store, target)
        raise ApiError(f"Das Kopieren ins Archiv ist fehlgeschlagen ({type(err).__name__}). Es wurde nichts verschoben, "
                       "das Album liegt unverändert im Regal.", 500)

    # Alles kopiert und geprueft: jetzt erst das Original entfernen
    try:
        shutil.rmtree(directory)
        parent = directory.parent
        if parent != OUTPUT and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as err:
        app.logger.exception("Original %s nach dem Archivieren nicht loeschbar", directory)
        raise ApiError("Das Album liegt vollständig und geprüft im Archiv, aber das Original im Regal ließ sich nicht "
                       f"löschen ({type(err).__name__}). Bitte den Ordner von Hand löschen.", 500)
    archive_index_after_move(rel.as_posix())
    return {"files": len(files), "bytes": total, "target": rel.as_posix(), "verified": verify}


# ---------------------------------------------------------------- Discogs-Zuordnung merken

DISCOGS_MAP_FILE = CONFIG / "discogs-cds.json"  # Disc-ID -> Discogs-Ausgabe und CD-Nummer
_map_lock = threading.Lock()


def load_discogs_map():
    try:
        return json.loads(DISCOGS_MAP_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def remember_discogs(path, root=None):
    """Nach dem Speichern: jede CD des Albums, die mit Discogs getaggt ist, unter ihrer Disc-ID merken."""
    try:
        directory = album_dir(path) if root is None else root / path
        entries = {}
        for number, _disc_dir, files in album_discs(directory):
            if not files:
                continue
            tags, _length = read_audio(files[0])
            disc_id, release = first(tags, "cddb").lower(), first(tags, "discogs_release_id")
            if disc_id and to_int(release):
                entries[disc_id] = {
                    "release": to_int(release),
                    "disc": number or to_int(first(tags, "discnumber")) or 1,
                    "artist": first(tags, "albumartist") or first(tags, "artist"),
                    "album": first(tags, "album"),
                    "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
        if entries:
            with _map_lock:
                known = load_discogs_map()
                known.update(entries)
                tmp = DISCOGS_MAP_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(known, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
                os.replace(tmp, DISCOGS_MAP_FILE)
    except Exception as err:  # Merken ist eine Zugabe; das Speichern selbst ist schon gelungen
        app.logger.warning("Discogs-Zuordnung für %s nicht gemerkt: %r", path, err)


def auto_discogs(disc_id):
    """Nach dem Rip: Ist diese CD schon einmal mit Discogs getaggt worden, die Daten von dort direkt uebernehmen."""
    entry = load_discogs_map().get((disc_id or "").lower())
    if not entry or not discogs_token():
        return False
    album = next((a for a in scan_albums() if a["disc_id"] == disc_id), None)
    if album is None or album["busy"] or album["incomplete"] or album["multi"]:
        return False
    try:
        release = normalize_release(discogs_get(f"/releases/{entry['release']}"))
        count = len(release["discs"])
        disc = next((d for d in release["discs"] if d["number"] == entry.get("disc", 1)), None)
        detail = album_detail(OUTPUT / album["path"])
        if disc is None or len(disc["tracks"]) != len(detail["tracks"]):
            app.logger.warning("Discogs-Ausgabe %s passt nicht mehr zu %s (Titelzahl)", entry["release"], album["path"])
            return False
        data = {
            "artist": release["artist"], "album": release["album"], "year": release["year"], "genre": release["genre"],
            "label": release["label"], "catno": release["catno"], "discogs_id": str(release["id"]),
            "discnumber": disc["number"] if count > 1 else "", "disctotal": count if count > 1 else "",
            "cover_url": release["cover"],
            "tracks": [{"file": tr["file"], "number": index, "title": info["title"], "artist": info["artist"]}
                       for index, (tr, info) in enumerate(zip(detail["tracks"], disc["tracks"]), start=1)],
        }
        try:
            path = save_album(OUTPUT / album["path"], data)
        except ApiError as err:
            if not data["cover_url"]:
                raise
            app.logger.warning("Discogs-Cover nicht übernommen (%s), speichere ohne", err.message)
            path = save_album(OUTPUT / album["path"], dict(data, cover_url=""))
        remember_discogs(path)
        app.logger.warning("Mit gemerkter Discogs-Ausgabe %s getaggt: %s", entry["release"], path)
        return True
    except ApiError as err:
        app.logger.warning("Gemerkte Discogs-Ausgabe für %s nicht übernommen: %s", album["path"], err.message)
    except Exception as err:
        app.logger.warning("Gemerkte Discogs-Ausgabe für %s nicht übernommen: %r", album["path"], err)
    return False


# ---------------------------------------------------------------- Cover nach dem Rip von selbst holen

def _plain(text):
    """Zum Vergleichen: nur Buchstaben und Ziffern, klein geschrieben."""
    return re.sub(r"[^0-9a-z]+", "", unicodedata.normalize("NFKD", str(text or "")).casefold())


def musicbrainz_cover(artist, album, tracks):
    """Frontcover zu einem Album aus dem Cover Art Archive holen (ueber die MusicBrainz-Suche). None, wenn es keins gibt."""
    quote = lambda text: re.sub(r'(["\\])', r"\\\1", text)
    query = f'release:"{quote(album)}"'
    if artist and _plain(artist) != "variousartists":
        query += f' AND artist:"{quote(artist)}"'
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(f"{MUSICBRAINZ_API}/release/", params={"query": query, "fmt": "json", "limit": 15}, headers=headers, timeout=20)
    resp.raise_for_status()
    releases = [r for r in resp.json().get("releases", [])
                if int(str(r.get("score", 0)) or 0) >= 90 and _plain(r.get("title")) == _plain(album)]
    # Ausgaben mit derselben Titelzahl zuerst: das ist am ehesten genau diese CD
    releases.sort(key=lambda r: r.get("track-count") != tracks)
    urls = [f"{COVERART_API}/release/{r['id']}/front-500" for r in releases[:4] if r.get("id")]
    groups = list(dict.fromkeys(r["release-group"]["id"] for r in releases if (r.get("release-group") or {}).get("id")))
    urls += [f"{COVERART_API}/release-group/{g}/front-500" for g in groups[:1]]
    for url in urls:
        time.sleep(1.1)  # MusicBrainz bittet um hoechstens eine Anfrage pro Sekunde
        image = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        mime = image.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if image.status_code == 200 and mime.startswith("image/") and 0 < len(image.content) <= MAX_DOWNLOAD_BYTES:
            return image.content, mime
    return None


def embed_cover(directory, cover):
    """Cover in alle Titel eines Albums schreiben und als Bilddatei daneben legen."""
    for _number, disc_dir, files in album_discs(directory):
        for path in files:
            if path.suffix.lower() == ".mp3":
                try:
                    tags = ID3(path)
                except ID3NoHeaderError:
                    tags = ID3()
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=cover[1], type=3, desc="Cover", data=cover[0]))
                tags.save(path, v2_version=3)
            else:
                audio = FLAC(path)
                picture = Picture()
                picture.type, picture.mime, picture.data = 3, cover[1], cover[0]
                audio.clear_pictures()
                audio.add_picture(picture)
                audio.save()
    write_cover_file(directory, cover)


def auto_cover(disc_id):
    """Nach einem fertigen Rip: fehlt der erkannten CD ein Cover, eins aus dem Cover Art Archive holen."""
    if not disc_id:
        return False
    album = next((a for a in scan_albums() if a["disc_id"] == disc_id), None)
    if album is None or album["unknown"] or album["incomplete"] or album["busy"]:
        return False
    directory = OUTPUT / album["path"]
    try:
        if album_cover(directory):
            return False
        cover = musicbrainz_cover(album["artist"], album["album"], album["tracks"])
        if not cover:
            app.logger.warning("Kein Cover im Cover Art Archive für %s", album["path"])
            return False
        data, mime, _size = shrink_cover(cover)
        embed_cover(directory, (data, mime))
        app.logger.warning("Cover automatisch geholt für %s", album["path"])
        return True
    except Exception as err:
        app.logger.warning("Cover für %s nicht automatisch ladbar: %r", album["path"], err)
        return False


# ---------------------------------------------------------------- Automatisch archivieren nach dem Rip

AUTO_EVENT = {"id": 0, "ok": True, "message": "", "album": ""}  # letztes Ergebnis, fuer die Meldung in der Oberflaeche


def auto_archive(disc_id):
    """Nach einem fertigen Rip: die CD mit dieser Disc-ID ins Archiv verschieben, wenn sie erkannt und vollstaendig ist."""
    if not archive_auto_on() or not disc_id:
        return None
    album = next((a for a in scan_albums() if a["disc_id"] == disc_id), None)
    if album is None or album["unknown"] or album["incomplete"] or album["multi"] or album["busy"]:
        return None  # nicht erkannt, unvollstaendig oder Teil eines Albums mit mehreren CDs: bleibt im Regal
    if LEGACY_DISC.match(Path(album["path"]).name):
        return None
    name = f"{album['artist']} - {album['album']}"
    try:
        archive_album(OUTPUT / album["path"])
        event = {"ok": True, "message": "", "album": name}
        app.logger.warning("Automatisch ins Archiv verschoben: %s", album["path"])
    except ApiError as err:
        event = {"ok": False, "message": err.message, "album": name}
        app.logger.warning("Automatisches Archivieren von %s fehlgeschlagen: %s", album["path"], err.message)
    except Exception as err:
        event = {"ok": False, "message": archive_error(err), "album": name}
        app.logger.exception("Automatisches Archivieren von %s fehlgeschlagen", album["path"])
    AUTO_EVENT.update(event, id=AUTO_EVENT["id"] + 1)
    return event


def watch_ripper():
    """Hintergrundfaden: bemerkt das Ende eines Rips und stoesst dann das automatische Archivieren an."""
    active_disc = ""
    while True:
        try:
            status = ripper_status()
            if status.get("state") in ACTIVE_STATES:
                active_disc = status.get("disc_id") or active_disc
            elif active_disc:
                disc, active_disc = active_disc, ""
                auto_discogs(disc)
                auto_cover(disc)
                auto_archive(disc)
        except Exception:
            app.logger.exception("Beobachten des Rippers fehlgeschlagen")
        time.sleep(3)


def backfill_discogs_map():
    """Beim Start: schon frueher mit Discogs getaggte Alben im Regal ebenfalls merken."""
    try:
        for album in scan_albums():
            if not album["unknown"]:
                remember_discogs(album["path"])
    except Exception as err:
        app.logger.warning("Discogs-Zuordnungen aus dem Regal nicht übernommen: %r", err)


def start_watcher():
    threading.Thread(target=backfill_discogs_map, name="discogs-backfill", daemon=True).start()
    threading.Thread(target=watch_ripper, name="ripper-watch", daemon=True).start()


# ---------------------------------------------------------------- Album als ZIP herunterladen

class _ZipSink:
    """Nimmt entgegen, was zipfile schreibt, damit es stueckweise an den Browser weitergereicht werden kann."""

    def __init__(self):
        self.parts, self.position = [], 0

    def write(self, data):
        self.parts.append(bytes(data))
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def drain(self):
        data, self.parts = b"".join(self.parts), []
        return data


def zip_stream(directory):
    """Albumordner als ZIP erzeugen (unkomprimiert - die Musik ist es schon) und dabei laufend ausliefern."""
    prefix = directory.relative_to(OUTPUT)  # im ZIP: Interpret/Album/...
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo.from_file(path, (prefix / path.relative_to(directory)).as_posix())
            info.compress_type = zipfile.ZIP_STORED
            with open(path, "rb") as source, archive.open(info, "w", force_zip64=True) as entry:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    entry.write(chunk)
                    yield sink.drain()
            yield sink.drain()
    yield sink.drain()


@app.get("/api/zip")
def api_zip():
    directory = album_dir(request.args.get("path"))
    discs = album_discs(directory)
    if not discs:
        raise ApiError("In diesem Ordner liegen keine Musikdateien.", 404)
    if album_summary(directory, discs, ripper_status())["busy"]:
        raise ApiError("Diese CD wird gerade gerippt. Bitte warten, bis der Rip fertig ist.", 409)
    name = safe_name(" - ".join(directory.relative_to(OUTPUT).parts), "Album") + ".zip"
    plain = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return Response(zip_stream(directory), mimetype="application/zip", headers={
        "Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{requests.utils.quote(name)}",
        "Cache-Control": "no-store",
    })


@app.post("/api/archive")
def api_archive():
    return jsonify(archive_album(album_dir(request.args.get("path"))))


@app.post("/api/delete")
def api_delete():
    """Album endgueltig aus dem Regal loeschen (die Oberflaeche fragt vorher nach)."""
    directory = album_dir(request.args.get("path"))
    discs = album_discs(directory)
    if not discs:
        raise ApiError("In diesem Ordner liegen keine Musikdateien.", 404)
    if album_summary(directory, discs, ripper_status())["busy"]:
        raise ApiError("Diese CD wird gerade gerippt. Bitte warten, bis der Rip fertig ist.", 409)
    if ARCHIVE_PROGRESS.get("path") == rel_id(directory):
        raise ApiError("Dieses Album wird gerade ins Archiv verschoben.", 409)
    files = sum(len(fs) for _number, _dir, fs in discs)
    try:
        shutil.rmtree(directory)
        parent = directory.parent
        if parent != OUTPUT and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as err:
        app.logger.warning("Album %s nicht loeschbar: %r", directory, err)
        raise ApiError("Das Album ließ sich nicht vollständig löschen. Vermutlich hat ein anderes Programm "
                       "den Ordner oder eine Datei geöffnet.", 409)
    app.logger.warning("Album geloescht: %s", rel_id(directory))
    return jsonify({"files": files})


@app.post("/api/archive/test")
def api_archive_test():
    """Archivziel aus dem Einstellungsdialog pruefen, noch bevor es gespeichert wird."""
    data = request.get_json(force=True) or {}
    kind, path = clean_archive_path(data.get("path"))
    saved = archive_settings()
    if kind == "smb":
        user = str(data.get("user") or "").strip()
        password = str(data.get("password") or "")
        if not password and saved["kind"] == "smb" and saved["path"].lower() == path.lower() and saved["user"] == user:
            password = saved["password"]  # leeres Feld: gespeichertes Kennwort verwenden
        test_archive({"kind": "smb", "path": path, "user": user, "password": password})
        return jsonify({"ok": True, "kind": "smb"})
    # lokaler Ordner: pruefbar ist nur, was gerade eingebunden ist
    if path.rstrip("\\").lower() != ARCHIVE_ACTIVE.rstrip("\\").lower():
        raise ApiError("Dieser Ordner ist noch nicht eingebunden und lässt sich deshalb noch nicht prüfen. "
                       "Speichere die Einstellungen und führe update.bat im cdripper-Ordner aus.")
    test_archive({"kind": "local", "path": path, "user": "", "password": ""})
    return jsonify({"ok": True, "kind": "local"})


@app.get("/api/attach")
def api_attach_preview():
    plan = attach_plan(request.args.get("source"), request.args.get("target"))
    return jsonify({key: value for key, value in plan.items() if not key.startswith("_")})


@app.post("/api/attach")
def api_attach():
    data = request.get_json(force=True) or {}
    path = attach_disc(data.get("source"), data.get("target"), to_int(data.get("number")), bool(data.get("use_discogs")))
    remember_discogs(path)
    return jsonify({"path": path})


@app.post("/api/cover/fetch")
def api_cover_fetch():
    """Cover von einer Web-Adresse holen (Cover-Suche, eingefuegte Bild-Adresse) und als Vorschau zurueckgeben."""
    url = str((request.get_json(force=True) or {}).get("url") or "")
    data, mime, size = shrink_cover(fetch_cover(url, any_host=True))
    return jsonify({
        "data": f"data:{mime};base64,{base64.b64encode(data).decode()}",
        "width": size[0] if size else None, "height": size[1] if size else None,
    })


@app.get("/api/settings")
def api_settings():
    return jsonify(settings_view())


@app.post("/api/settings")
def api_settings_save():
    data = request.get_json(force=True) or {}
    token = "".join(str(data.get("discogs_token") or "").split())
    fmt = str(data.get("format") or "")
    if fmt and fmt not in FORMATS:
        raise ApiError("Unbekanntes Format.")
    if "language" in data and data["language"] not in ("de", "en"):
        raise ApiError("Unbekannte Sprache.")
    if "language" in data:  # zuerst, damit auch die Antwort auf diese Anfrage schon in der neuen Sprache kommt
        settings = load_settings()
        settings["language"] = data["language"]
        save_settings(settings)
    if "archive_path" in data:
        set_archive(data["archive_path"], data.get("archive_user"), data.get("archive_password"))
    if "archive_verify" in data or "archive_auto" in data:
        settings = load_settings()
        if "archive_verify" in data:
            settings["archive_verify"] = bool(data["archive_verify"])
        if "archive_auto" in data:
            settings["archive_auto"] = bool(data["archive_auto"])
        save_settings(settings)
    user = None
    if token:  # leeres Feld = Token unveraendert lassen
        user = check_discogs_token(token)  # bricht ab, bevor irgendetwas gespeichert wird
        settings = load_settings()
        settings["discogs_token"] = token
        save_settings(settings)
    if fmt:
        set_rip_format(fmt)
    if "year_prefix" in data:
        set_year_prefix(bool(data["year_prefix"]))
    view = settings_view()
    if user is not None:
        view["discogs_user"] = user
    return jsonify(view)


@app.get("/api/audio")
def api_audio():
    """Einen Titel zum Probehoeren ausliefern (mit Range-Unterstuetzung zum Spulen)."""
    directory = album_dir(request.args.get("path"))
    name = request.args.get("file") or ""
    path = next((p for _number, _dir, files in album_discs(directory) for p in files
                 if p.relative_to(directory).as_posix() == name), None)
    if path is None:
        raise ApiError("Titel nicht gefunden.", 404)
    mimetype = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/flac"
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)


@app.get("/api/discogs/search")
def api_discogs_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        raise ApiError("Bitte einen Suchbegriff eingeben.")
    params = {"type": "release", "per_page": 30}
    digits = re.sub(r"[\s-]", "", query)
    if digits.isdigit() and len(digits) >= 8:
        params["barcode"] = digits
    else:
        params["q"] = query
    if request.args.get("cd", "1") == "1":
        params["format"] = "CD"
    data = discogs_get("/database/search", params)
    results = [{
        "id": r.get("id"),
        "title": r.get("title") or "",
        "year": r.get("year") or "",
        "country": r.get("country") or "",
        "label": (r.get("label") or [""])[0],
        "catno": r.get("catno") or "",
        "format": ", ".join(r.get("format") or []),
        "thumb": r.get("thumb") or "",
    } for r in data.get("results") or []]
    return jsonify({"results": results, "total": (data.get("pagination") or {}).get("items", len(results))})


@app.get("/api/discogs/release/<int:release_id>")
def api_discogs_release(release_id):
    return jsonify(normalize_release(discogs_get(f"/releases/{release_id}")))


# ---------------------------------------------------------------- Archiv ansehen und bearbeiten
#
# Das Archiv kann gross sein und im Netz liegen. Deshalb wird es einmal eingelesen und in einer kleinen
# Datenbank (config/archive.db) gemerkt: je Album Interpret, Titel, Jahr, Titelzahl und ein kleines Cover.
# Beim Neueinlesen werden nur Alben gelesen, deren Dateien sich geaendert haben.
# Bearbeitet wird ueber einen Arbeitsordner: Album holen, wie im Regal taggen, zurueckschreiben.

ARCHIVE_DB = CONFIG / "archive.db"
NEW_SUFFIX = ".shelfripper-neu"  # neue Dateien heissen so, bis die alten ersetzt sind
COVER_NAMES = ("cover.jpg", "folder.jpg", "cover.png")
_db_lock = threading.Lock()
_db_conn = None
SCAN_STATE = {"running": False, "done": 0, "total": 0, "error": "", "finished": 0}


def archive_db():
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(ARCHIVE_DB, check_same_thread=False)
        _db_conn.execute("""CREATE TABLE IF NOT EXISTS albums (
            path TEXT PRIMARY KEY, artist TEXT, album TEXT, year TEXT, genre TEXT, tracks INTEGER,
            track_total INTEGER, discs INTEGER, format TEXT, disc_id TEXT, discogs_id TEXT,
            signature TEXT, thumb BLOB, scanned REAL)""")
        _db_conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        _db_conn.commit()
    return _db_conn


def archive_identity():
    target = archive_settings()
    return f"{target['kind']}:{target['path'].lower()}" if target["kind"] == "smb" else f"local:{ARCHIVE_ACTIVE}"


def db_check_identity():
    """Wurde ein anderes Archiv eingestellt, gilt die gemerkte Liste nicht mehr."""
    with _db_lock:
        db = archive_db()
        row = db.execute("SELECT value FROM meta WHERE key='archive'").fetchone()
        current = archive_identity()
        if not row or row[0] != current:
            db.execute("DELETE FROM albums")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('archive', ?)", (current,))
            db.execute("DELETE FROM meta WHERE key='scanned'")
            db.commit()


def archive_parts(rel):
    """Albumpfad aus der Oberflaeche pruefen und als Teile liefern."""
    parts = tuple(p for p in str(rel or "").replace("\\", "/").split("/") if p)
    if not parts:
        raise ApiError("Kein Album angegeben.")
    if any(p in (".", "..") or p.startswith(".") or INVALID_CHARS.search(p) for p in parts):
        raise ApiError("Ungültiger Pfad.")
    return parts


def is_audio_name(name):
    return Path(name).suffix.lower() in AUDIO_SUFFIXES and not name.startswith(".")


def store_album_listing(store, parts, entries=None):
    """Dateien eines Archiv-Albums: [(relativer Name, groesse, zeit)] und die CD-Nummern; None, wenn es keins ist."""
    entries = store.scandir(parts) if entries is None else entries
    if any(name == ARCHIVE_MARKER for name, *_ in entries):
        return None  # Kopie noch nicht fertig
    files = [(name, size, mtime) for name, is_dir, size, mtime in entries if not is_dir]
    if any(is_audio_name(name) for name, *_ in files):
        return {"files": sorted(files), "discs": [None]}
    discs, listing = [], [(name, size, mtime) for name, size, mtime in files]
    for name, is_dir, _size, _mtime in entries:
        match = DISC_DIR.match(name) if is_dir else None
        if not match:
            continue
        sub = [(f"{name}/{n}", sz, mt) for n, d, sz, mt in store.scandir(parts + (name,)) if not d]
        if any(is_audio_name(n.split("/", 1)[1]) for n, *_ in sub):
            discs.append(int(match.group(1)))
            listing += sub
    if not discs:
        return None
    return {"files": sorted(listing), "discs": sorted(discs)}


def listing_signature(listing):
    raw = "\n".join(f"{n}|{s}|{int(m)}" for n, s, m in listing["files"])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def read_audio_obj(handle, name):
    """Wie read_audio, aber aus einer geoeffneten Datei (auch auf der Netzwerkfreigabe)."""
    if name.lower().endswith(".mp3"):
        audio = MP3(handle)
        id3 = audio.tags or {}
        tags = {key: [str(t) for t in id3[frame].text] for key, frame in ID3_TEXT.items() if frame in id3}
        if "TCON" in id3:
            tags["genre"] = list(id3["TCON"].genres)
        for frame, number, total in (("TRCK", "tracknumber", "tracktotal"), ("TPOS", "discnumber", "disctotal")):
            if frame in id3:
                parts = str(id3[frame].text[0]).split("/")
                tags[number] = [parts[0]]
                if len(parts) > 1:
                    tags[total] = [parts[1]]
        for frame in (id3.getall("TXXX") if id3 else []):
            tags[frame.desc.lower()] = [str(t) for t in frame.text]
        pictures = [(f.data, f.mime) for f in id3.getall("APIC")] if id3 else []
    else:
        audio = FLAC(handle)
        tags = audio.tags.as_dict() if audio.tags else {}
        pictures = [(p.data, p.mime) for p in audio.pictures]
    return tags, (round(audio.info.length) if audio.info else 0), pictures


def store_read_audio(store, parts, name):
    with store.open(parts + tuple(name.split("/")), "rb") as handle:
        return read_audio_obj(handle, name)


def store_cover(store, parts, listing):
    """Cover eines Archiv-Albums: Bilddatei im Ordner, sonst das in den ersten Titel eingebettete Bild."""
    names = {n.lower(): n for n, *_ in listing["files"]}
    for wanted in COVER_NAMES:
        for prefix in ("",) + tuple(f"cd{d}/" for d in listing["discs"] if d):
            real = names.get(prefix + wanted)
            if real:
                with store.open(parts + tuple(real.split("/")), "rb") as handle:
                    return handle.read(), "image/png" if real.lower().endswith(".png") else "image/jpeg"
    first_audio = next((n for n, *_ in listing["files"] if is_audio_name(n.rsplit("/", 1)[-1])), None)
    if first_audio:
        pictures = store_read_audio(store, parts, first_audio)[2]
        if pictures:
            return pictures[0][0], pictures[0][1] or "image/jpeg"
    return None


def index_archive_album(store, parts, listing=None):
    """Ein Album aus dem Archiv lesen und in der Datenbank merken (oder entfernen, wenn es keins mehr ist)."""
    rel = "/".join(parts)
    listing = listing or (store_album_listing(store, parts) if store.exists(parts) else None)
    if not listing:
        with _db_lock:
            archive_db().execute("DELETE FROM albums WHERE path=?", (rel,))
            archive_db().commit()
        return None
    audio = [n for n, *_ in listing["files"] if is_audio_name(n.rsplit("/", 1)[-1])]
    tags, _length, pictures = store_read_audio(store, parts, audio[0])
    thumb = None
    try:
        names = {n.lower() for n, *_ in listing["files"]}
        cover = None
        if not pictures or any(c in names for c in COVER_NAMES):
            cover = store_cover(store, parts, listing)
        elif pictures:
            cover = (pictures[0][0], pictures[0][1] or "image/jpeg")
        if cover:
            thumb = cover_thumb(cover, 240)[0]
    except Exception as err:
        app.logger.warning("Cover von %s im Archiv nicht lesbar: %r", rel, err)
    multi = listing["discs"] != [None]
    row = (
        rel, first(tags, "albumartist") or first(tags, "artist") or (parts[0] if len(parts) > 1 else ""),
        first(tags, "album") or parts[-1], first(tags, "date")[:4], "; ".join(tags.get("genre", [])),
        len(audio), None if multi else to_int(first(tags, "tracktotal")), len(listing["discs"]) if multi else 0,
        "/".join(sorted({Path(n).suffix.lower()[1:].upper() for n in audio})), first(tags, "cddb").lower(),
        first(tags, "discogs_release_id"), listing_signature(listing), thumb, time.time(),
    )
    with _db_lock:
        archive_db().execute("INSERT OR REPLACE INTO albums VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        archive_db().commit()
    return rel


def walk_archive(store):
    """Alle Alben im Archiv finden (Interpret/Album oder tiefer) - liefert [(teile, listing, eintraege)]."""
    found = []

    def visit(parts, depth):
        try:
            entries = store.scandir(parts)
        except Exception as err:
            app.logger.warning("Archivordner %s nicht lesbar: %r", "/".join(parts) or "/", err)
            return
        if parts:
            listing = store_album_listing(store, parts, entries)
            if listing:
                found.append((parts, listing))
                return
            if any(name == ARCHIVE_MARKER for name, *_ in entries):
                return
        if depth >= 4:
            return
        for name, is_dir, _size, _mtime in sorted(entries):
            if is_dir and not name.startswith(".") and not (parts and DISC_DIR.match(name)):
                visit(parts + (name,), depth + 1)

    visit((), 0)
    return found


def scan_archive():
    """Archiv einlesen (im Hintergrund). Unveraenderte Alben werden uebersprungen."""
    if SCAN_STATE["running"]:
        return
    SCAN_STATE.update(running=True, done=0, total=0, error="")
    try:
        db_check_identity()
        store = archive_store()
        store.check()
        albums = walk_archive(store)
        SCAN_STATE["total"] = len(albums)
        with _db_lock:
            known = dict(archive_db().execute("SELECT path, signature FROM albums").fetchall())
        seen = set()
        for parts, listing in albums:
            rel = "/".join(parts)
            seen.add(rel)
            if known.get(rel) != listing_signature(listing):
                try:
                    index_archive_album(store, parts, listing)
                except Exception as err:
                    app.logger.warning("Album %s im Archiv nicht lesbar: %r", rel, err)
            SCAN_STATE["done"] += 1
        with _db_lock:
            db = archive_db()
            for rel in set(known) - seen:
                db.execute("DELETE FROM albums WHERE path=?", (rel,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('scanned', ?)", (str(time.time()),))
            db.commit()
    except ApiError as err:
        SCAN_STATE["error"] = err.message
    except Exception as err:
        app.logger.warning("Archiv nicht einlesbar: %r", err)
        SCAN_STATE["error"] = archive_error(err)
    finally:
        SCAN_STATE.update(running=False, finished=time.time())


def start_archive_scan():
    if not SCAN_STATE["running"]:
        threading.Thread(target=scan_archive, name="archive-scan", daemon=True).start()


def archive_index_after_move(rel):
    """Nach dem Verschieben ins Archiv das Album gleich in die Liste aufnehmen."""
    try:
        db_check_identity()
        index_archive_album(archive_store(), archive_parts(rel))
    except Exception as err:
        app.logger.warning("Archiv-Liste für %s nicht aktualisiert: %r", rel, err)


def archive_row(row):
    (path, artist, album, year, genre, tracks, track_total, discs, fmt, disc_id, discogs_id, signature) = row
    folder = path.rsplit("/", 1)[-1]
    return {
        "path": path, "artist": artist or "", "album": album or "", "year": year or "", "genre": genre or "",
        "tracks": tracks or 0, "track_total": track_total, "discs": discs or 0, "multi": bool(discs),
        "format": fmt or "", "disc_id": disc_id or "", "discogs_id": discogs_id or "", "modified": signature,
        "unknown": is_unknown(artist or "", album or "", Path(folder)), "busy": False,
        "incomplete": bool(track_total and not discs and track_total > (tracks or 0)), "source": "archive",
    }


@app.get("/api/archive/albums")
def api_archive_albums():
    db_check_identity()
    with _db_lock:
        rows = archive_db().execute("SELECT path, artist, album, year, genre, tracks, track_total, discs, format, "
                                    "disc_id, discogs_id, signature FROM albums").fetchall()
        scanned = archive_db().execute("SELECT value FROM meta WHERE key='scanned'").fetchone()
    if not scanned and not SCAN_STATE["running"] and not SCAN_STATE["error"]:
        start_archive_scan()  # zum ersten Mal: von selbst einlesen
    albums = [archive_row(r) for r in rows]
    albums.sort(key=lambda a: (not a["unknown"], a["artist"].lower(), a["album"].lower(), a["path"]))
    return jsonify({"albums": albums, "scan": dict(SCAN_STATE, error=translate(SCAN_STATE["error"])),
                    "scanned": float(scanned[0]) if scanned else None})


@app.post("/api/archive/rescan")
def api_archive_rescan():
    start_archive_scan()
    return jsonify({"ok": True})


def archive_listing_or_404(store, parts):
    try:
        listing = store_album_listing(store, parts) if store.exists(parts) else None
    except ApiError:
        raise
    except Exception as err:
        raise ApiError(archive_error(err), 502)
    if not listing:
        index_archive_album(store, parts, None)  # aus der Liste nehmen
        raise ApiError("Album nicht gefunden.", 404)
    return listing


@app.get("/api/archive/album")
def api_archive_album():
    parts = archive_parts(request.args.get("path"))
    store = archive_store()
    listing = archive_listing_or_404(store, parts)
    tracks, head = [], None
    for name, *_ in listing["files"]:
        if not is_audio_name(name.rsplit("/", 1)[-1]):
            continue
        tags, length, _pictures = store_read_audio(store, parts, name)
        head = head or tags
        disc = to_int(name.split("/", 1)[0][2:]) if "/" in name else None
        tracks.append({"file": name, "disc": disc,
                       "number": to_int(first(tags, "tracknumber")) or len([t for t in tracks if t["disc"] == disc]) + 1,
                       "title": first(tags, "title"), "artist": first(tags, "artist"), "length": length})
    multi = listing["discs"] != [None]
    artist = first(head, "albumartist") or first(head, "artist")
    album = first(head, "album")
    total = to_int(first(head, "tracktotal"))
    signature = listing_signature(listing)
    return jsonify({
        "path": "/".join(parts), "artist": artist, "album": album, "year": first(head, "date")[:4],
        "tracks": tracks, "track_total": None if multi else total, "multi": multi,
        "discs": len(listing["discs"]) if multi else 0, "unknown": is_unknown(artist, album, Path(parts[-1])),
        "busy": False, "incomplete": bool(total and not multi and total > len(tracks)),
        "disc_id": first(head, "cddb"), "modified": signature, "source": "archive",
        "format": "/".join(sorted({Path(t["file"]).suffix.lower()[1:].upper() for t in tracks})),
        "genre": "; ".join(head.get("genre", [])),
        "discnumber": "" if multi else to_int(first(head, "discnumber")) or "",
        "disctotal": max(to_int(first(head, "disctotal")) or 0, listing["discs"][-1], len(listing["discs"])) if multi
        else to_int(first(head, "disctotal")) or "",
        "label": first(head, "label"), "catno": first(head, "catalognumber"),
        "discogs_id": first(head, "discogs_release_id"),
    })


@app.get("/api/archive/cover")
def api_archive_cover():
    parts = archive_parts(request.args.get("path"))
    edge = to_int(request.args.get("size")) or 0
    cover = None
    if edge:
        with _db_lock:
            row = archive_db().execute("SELECT thumb FROM albums WHERE path=?", ("/".join(parts),)).fetchone()
        cover = (row[0], "image/jpeg") if row and row[0] else None
    else:
        store = archive_store()
        cover = store_cover(store, parts, archive_listing_or_404(store, parts))
    if not cover:
        return Response(status=404)
    response = Response(cover[0], mimetype=cover[1])
    response.headers["Cache-Control"] = "max-age=86400" if edge and request.args.get("t") else "no-cache"
    return response


@app.get("/api/archive/audio")
def api_archive_audio():
    """Titel aus dem Archiv zum Probehoeren ausliefern, mit Spulen (Range)."""
    parts = archive_parts(request.args.get("path"))
    name = request.args.get("file") or ""
    store = archive_store()
    listing = archive_listing_or_404(store, parts)
    entry = next(((n, size) for n, size, _m in listing["files"] if n == name and is_audio_name(n.rsplit("/", 1)[-1])), None)
    if entry is None:
        raise ApiError("Titel nicht gefunden.", 404)
    size = entry[1]
    start, end = 0, size - 1
    match = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:
            start = max(size - int(match.group(2)), 0)
    if start > end:
        return Response(status=416, headers={"Content-Range": f"bytes */{size}"})

    def body():
        with store.open(parts + tuple(name.split("/")), "rb") as handle:
            handle.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = handle.read(min(256 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-cache"}
    status = 200
    if match and (match.group(1) or match.group(2)):
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(body(), status=status, headers=headers,
                    mimetype="audio/mpeg" if name.lower().endswith(".mp3") else "audio/flac")


def copy_from_store(store, parts, listing, target, progress=None, verify=False):
    """Album aus dem Archiv in einen lokalen Ordner kopieren (optional mit Pruefsumme)."""
    total = sum(size for _n, size, _m in listing["files"]) or 1
    done = 0
    for index, (name, _size, _mtime) in enumerate(listing["files"], 1):
        local = target.joinpath(*name.split("/"))
        local.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with store.open(parts + tuple(name.split("/")), "rb") as reader, open(local, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(index, len(listing["files"]), done * 100 // total)
        if verify:
            check = hashlib.sha256(local.read_bytes()).hexdigest()
            if check != digest.hexdigest():
                raise ApiError(f"Die Prüfsumme von '{name}' stimmt nach dem Kopieren nicht.", 500)


def remove_store_album(store, parts, listing):
    """Die Dateien eines Archiv-Albums loeschen und leer gewordene Ordner entfernen."""
    for name, *_ in listing["files"]:
        store.remove(parts + tuple(name.split("/")))
    for disc in listing["discs"]:
        if disc is not None:
            for name, is_dir, *_ in store.scandir(parts):
                if is_dir and DISC_DIR.match(name) and int(DISC_DIR.match(name).group(1)) == disc:
                    store.rmdir_if_empty(parts + (name,))
    for depth in range(len(parts), 0, -1):
        try:
            store.rmdir_if_empty(parts[:depth])
        except Exception:
            break


def store_move_file(store, temp, dest):
    """Datei im Archiv umbenennen; kann der Server das nicht, umkopieren."""
    try:
        store.rename(temp, dest)
        return
    except Exception as err:
        app.logger.info("Umbenennen im Archiv nicht möglich (%r), kopiere stattdessen", err)
    with store.open(temp, "rb") as reader, store.open(dest, "wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
    store.remove(temp)


ARCHIVE_EDIT_LOCK = threading.Lock()


@app.post("/api/archive/album")
def api_archive_album_save():
    """Album im Archiv neu taggen: holen, wie im Regal bearbeiten, Ergebnis zurueckschreiben."""
    parts = archive_parts(request.args.get("path"))
    data = request.get_json(force=True) or {}
    if not ARCHIVE_EDIT_LOCK.acquire(blocking=False):
        raise ApiError("Im Archiv wird gerade schon etwas gespeichert. Bitte kurz warten.", 409)
    work = Path(tempfile.mkdtemp(prefix="shelfripper-"))
    try:
        store = archive_store()
        listing = archive_listing_or_404(store, parts)
        old_rel = "/".join(parts)
        try:
            copy_from_store(store, parts, listing, work.joinpath(*parts))
        except ApiError:
            raise
        except Exception as err:
            raise ApiError(archive_error(err), 502)
        new_rel = save_album(work.joinpath(*parts), data, root=work)
        remember_discogs(new_rel, root=work)
        new_dir = work.joinpath(*new_rel.split("/"))
        # die Dateien des Ergebnisses (bei einem Mehrfach-Album nur die eben einsortierte CD)
        produced = sorted(p for p in work.rglob("*") if p.is_file() and ".cdripper" not in p.relative_to(work).parts)
        holder = Path(os.path.commonpath([str(p.parent) for p in produced]))  # Album- bzw. eben einsortierter CD-Ordner
        target = tuple(holder.relative_to(work).parts)
        if "/".join(target).lower() != old_rel.lower() and store.exists(target):
            raise ApiError(f"'{'/'.join(target)}' gibt es im Archiv schon. Es wurde nichts geändert.", 409)
        if len(target) > len(tuple(new_rel.split("/"))):  # einsortiert als CDn: Album darf keine Einzel-CD sein
            album_parts = tuple(new_rel.split("/"))
            if store.exists(album_parts) and any(is_audio_name(n) for n, d, *_ in store.scandir(album_parts) if not d):
                raise ApiError(f"'{new_rel}' liegt im Archiv als einzelne CD. Bitte dort zuerst die CD-Nummer eintragen.", 409)
        verify = archive_verify_on()
        uploaded = []
        try:
            for path in produced:
                dest = tuple(path.relative_to(work).parts)
                store.makedirs(dest[:-1])
                temp = dest[:-1] + (dest[-1] + NEW_SUFFIX,)
                digest = hashlib.sha256()
                with open(path, "rb") as reader, store.open(temp, "wb") as writer:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        digest.update(chunk)
                        writer.write(chunk)
                uploaded.append((temp, dest))
                if verify:
                    check = hashlib.sha256()
                    with store.open(temp, "rb") as reader:
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            check.update(chunk)
                    if check.hexdigest() != digest.hexdigest():
                        raise ApiError(f"Die Prüfsumme von '{'/'.join(dest)}' stimmt nach dem Schreiben nicht. "
                                       "Im Archiv wurde nichts geändert.", 500)
        except Exception as err:
            for temp, _dest in uploaded:
                try:
                    store.remove(temp)
                except Exception:
                    pass
            if isinstance(err, ApiError):
                raise
            raise ApiError(archive_error(err), 502)
        # Neue Fassung liegt vollstaendig im Archiv: alte Dateien weg, neue an ihren Platz
        try:
            remove_store_album(store, parts, listing)
            for temp, dest in uploaded:
                store_move_file(store, temp, dest)
        except Exception as err:
            app.logger.exception("Archiv-Album %s nicht vollständig ersetzt", old_rel)
            raise ApiError("Die neue Fassung liegt im Archiv, aber das Ersetzen der alten Dateien ist mittendrin "
                           f"gescheitert ({type(err).__name__}). Bitte „Archiv neu einlesen“ und den Ordner prüfen.", 500)
        with _db_lock:
            archive_db().execute("DELETE FROM albums WHERE path=?", (old_rel,))
            archive_db().commit()
        index_archive_album(store, tuple(new_rel.split("/")))
        return jsonify({"path": new_rel})
    finally:
        shutil.rmtree(work, ignore_errors=True)
        ARCHIVE_EDIT_LOCK.release()


@app.post("/api/archive/restore")
def api_archive_restore():
    """Album aus dem Archiv zurueck ins Regal holen (erst kopieren, dann im Archiv loeschen)."""
    parts = archive_parts(request.args.get("path"))
    store = archive_store()
    listing = archive_listing_or_404(store, parts)
    target = OUTPUT.joinpath(*parts)
    if target.exists():
        raise ApiError(f"'{'/'.join(parts)}' gibt es im Regal schon. Es wurde nichts verschoben.", 409)
    verify = archive_verify_on()

    def progress(index, count, percent):
        ARCHIVE_PROGRESS.update(path="/".join(parts), name=" - ".join(parts), file=index, files=count,
                                phase="restore", percent=percent)
    try:
        copy_from_store(store, parts, listing, target, progress, verify)
    except Exception as err:
        shutil.rmtree(target, ignore_errors=True)
        if target.parent != OUTPUT and target.parent.is_dir() and not any(target.parent.iterdir()):
            target.parent.rmdir()
        if isinstance(err, ApiError):
            raise
        raise ApiError(archive_error(err), 502)
    finally:
        ARCHIVE_PROGRESS.clear()
    try:
        remove_store_album(store, parts, listing)
    except Exception as err:
        app.logger.exception("Archiv-Album %s nach dem Zurueckholen nicht loeschbar", "/".join(parts))
        raise ApiError("Das Album liegt wieder im Regal, ließ sich im Archiv aber nicht löschen "
                       f"({type(err).__name__}). Bitte dort von Hand löschen.", 500)
    finally:
        index_archive_album(store, parts, None)
    return jsonify({"path": "/".join(parts), "files": len(listing["files"])})


@app.post("/api/archive/delete")
def api_archive_delete():
    parts = archive_parts(request.args.get("path"))
    store = archive_store()
    listing = archive_listing_or_404(store, parts)
    try:
        remove_store_album(store, parts, listing)
    except Exception as err:
        app.logger.warning("Archiv-Album %s nicht loeschbar: %r", "/".join(parts), err)
        raise ApiError(archive_error(err), 502)
    finally:
        index_archive_album(store, parts, None)
    app.logger.warning("Album im Archiv geloescht: %s", "/".join(parts))
    return jsonify({"files": len(listing["files"])})


@app.get("/api/archive/zip")
def api_archive_zip():
    parts = archive_parts(request.args.get("path"))
    store = archive_store()
    listing = archive_listing_or_404(store, parts)

    def stream():
        sink = _ZipSink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
            for name, _size, mtime in listing["files"]:
                info = zipfile.ZipInfo("/".join(parts + tuple(name.split("/"))), time.localtime(mtime)[:6])
                info.compress_type = zipfile.ZIP_STORED
                with store.open(parts + tuple(name.split("/")), "rb") as source, archive.open(info, "w", force_zip64=True) as entry:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        entry.write(chunk)
                        yield sink.drain()
                yield sink.drain()
        yield sink.drain()

    name = safe_name(" - ".join(parts), "Album") + ".zip"
    plain = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return Response(stream(), mimetype="application/zip", headers={
        "Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{requests.utils.quote(name)}",
        "Cache-Control": "no-store",
    })



if __name__ == "__main__":
    from waitress import serve
    start_watcher()
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
