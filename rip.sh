#!/bin/bash
# Ueberwacht das Laufwerk und rippt jede eingelegte Audio-CD nach /output als FLAC.

DEVICE="${DEVICE:-/dev/sr0}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
EJECT="${EJECT:-true}"
OUTPUT="/output"
MP3_QUALITY="${MP3_QUALITY:-0}"    # lame -V: 0 = beste Qualitaet (ca. 245 kbit/s), 2 = ca. 190 kbit/s

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# CD auswerfen. "eject" scheitert, wenn das Laufwerk als eingebundene Geraetedatei im Container steckt
# (Docker in einem LXC-Container unter Proxmox); dann direkt per SCSI-Befehl: Sperre loesen und auswerfen.
eject_disc() {
    eject "$DEVICE" 2>/dev/null && return 0
    command -v sg_start >/dev/null 2>&1 || return 1
    sg_prevent --allow "$DEVICE" >/dev/null 2>&1
    sg_start --eject "$DEVICE" >/dev/null 2>&1
}

STATE_DIR="$OUTPUT/.cdripper"
CANCEL_FILE="$STATE_DIR/cancel"     # legt die Weboberflaeche an, um den laufenden Rip abzubrechen

# Status fuer die Weboberflaeche: was passiert gerade, welcher Titel, wie weit?
# set_status <zustand> <disc-id> [titel_gesamt] [aktueller_titel] [prozent]
# Interpret und Album der laufenden CD stehen in STATUS_TITLE ("Interpret / Album", wie abcde es liefert).
set_status() {
    local title="${STATUS_TITLE:-}"
    [ "$1" = "idle" ] && title=""
    # fuer JSON entschaerfen: Steuerzeichen und ungueltige Bytes entfernen, \ und " maskieren
    title=$(printf '%s' "$title" | tr -d '\000-\037' | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null)
    title="${title//\\/\\\\}"; title="${title//\"/\\\"}"
    mkdir -p "$STATE_DIR" 2>/dev/null
    printf '{"state":"%s","disc_id":"%s","mp3":true,"cancel":true,"track_total":%d,"track":%d,"percent":%d,"title":"%s"}\n' \
        "$1" "$2" "${3:-0}" "${4:-0}" "${5:-0}" "$title" > "$STATE_DIR/status.json.tmp" 2>/dev/null \
        && mv -f "$STATE_DIR/status.json.tmp" "$STATE_DIR/status.json" 2>/dev/null
}

# Gewuenschtes Format (flac oder mp3): Einstellung aus der Weboberflaeche, sonst Umgebungsvariable FORMAT
wanted_format() {
    local f=""
    [ -r "$OUTPUT/.cdripper/format" ] && f=$(tr -d '[:space:]' < "$OUTPUT/.cdripper/format")
    f="${f:-${FORMAT:-flac}}"
    [ "$f" = "mp3" ] && echo mp3 || echo flac
}

flac_tag() { metaflac --show-tag="$2" "$1" 2>/dev/null | head -n 1 | cut -d= -f2-; }

# Die FLAC-Dateien des laufenden Rips auflisten (durch NUL getrennt): neuer als die Markerdatei $1 UND
# mit der Disc-ID $2 in den Tags. Das Alter allein reicht nicht - wer waehrend des Rips in der Weboberflaeche
# ein anderes Album speichert, macht dessen Dateien ebenfalls "neu".
rip_files() {
    local marker="$1" disc="$2" flac
    while IFS= read -r -d '' flac; do
        [ "$(flac_tag "$flac" CDDB)" = "$disc" ] && printf '%s\0' "$flac"
    done < <(find "$OUTPUT" -type f -name '*.flac' -newer "$marker" -not -path '*/.*' -print0 | sort -z)
}

# Wandelt die FLAC-Dateien des laufenden Rips (Markerdatei $1, Disc-ID in STATUS_DISC) in MP3 um.
# Die FLAC-Dateien werden erst geloescht, wenn ALLE MP3s der CD fertig sind;
# geht etwas schief, bleibt die CD als FLAC erhalten.
convert_to_mp3() {
    local marker="$1" flac part title artist albumartist album track total year cddb i
    local -a flacs=() args
    while IFS= read -r -d '' flac; do
        flacs+=("$flac")
    done < <(rip_files "$marker" "$STATUS_DISC")
    if [ "${#flacs[@]}" -eq 0 ]; then
        log "MP3: keine neuen Dateien gefunden."
        return 0
    fi

    i=0
    for flac in "${flacs[@]}"; do
        i=$((i + 1))
        set_status converting "$STATUS_DISC" "${#flacs[@]}" "$i" $(( (i - 1) * 100 / ${#flacs[@]} ))
        part="${flac%.flac}.mp3.part"
        title=$(flac_tag "$flac" TITLE);   artist=$(flac_tag "$flac" ARTIST)
        album=$(flac_tag "$flac" ALBUM);   albumartist=$(flac_tag "$flac" ALBUMARTIST)
        track=$(flac_tag "$flac" TRACKNUMBER); total=$(flac_tag "$flac" TRACKTOTAL)
        year=$(flac_tag "$flac" DATE); year="${year:0:4}"
        cddb=$(flac_tag "$flac" CDDB)

        args=(--quiet -V "$MP3_QUALITY" --id3v2-only --id3v2-utf16 --ignore-tag-errors)
        [ -n "$title" ]  && args+=(--tt "$title")
        [ -n "$artist" ] && args+=(--ta "$artist")
        [ -n "$album" ]  && args+=(--tl "$album")
        [ -n "$albumartist" ] && args+=(--tv "TPE2=$albumartist")
        [ -n "$track" ]  && args+=(--tn "${track}${total:+/$total}")
        [[ "$year" =~ ^[0-9]{4}$ ]] && args+=(--ty "$year")
        [ -n "$cddb" ]   && args+=(--tv "TXXX=CDDB=$cddb")

        if ! flac --decode --stdout --silent "$flac" | lame "${args[@]}" - "$part"; then
            log "FEHLER: MP3 fuer '${flac#"$OUTPUT"/}' konnte nicht erzeugt werden. Die CD bleibt als FLAC erhalten."
            for flac in "${flacs[@]}"; do rm -f "${flac%.flac}.mp3.part"; done
            return 1
        fi
    done

    # Alles fertig: MP3s freigeben, FLACs entfernen
    for flac in "${flacs[@]}"; do
        mv -f "${flac%.flac}.mp3.part" "${flac%.flac}.mp3" && rm -f "$flac"
    done
    log "MP3: ${#flacs[@]} Datei(en) erzeugt, FLAC-Dateien entfernt."
}

# Fortschritt des laufenden Rips aus dem Arbeitsordner von abcde ablesen und in die Statusdatei schreiben.
# Gezaehlt werden fertig gelesene Titel; die wachsende WAV-Datei des aktuellen Titels macht die Anzeige fluessig.
report_progress() {
    local disc="$1" total="$2" workdir statusfile read_done current wav size frames_done i percent f
    workdir=$(ls -d /tmp/abcde.* 2>/dev/null | head -n 1)
    statusfile="$workdir/status"
    read_done=0
    [ -n "$workdir" ] && [ -r "$statusfile" ] && read_done=$(grep -c '^readtrack-' "$statusfile" 2>/dev/null)
    read_done="${read_done:-0}"
    [ "$read_done" -gt "$total" ] && read_done="$total"
    # Interpret und Album: abcde legt das Ergebnis der Datenbankabfrage als cddbread.N ab.
    # cddbread.0 ist der Platzhalter fuer "unbekannt" und zaehlt erst, wenn schon ein Titel gelesen ist.
    if [ -z "$STATUS_TITLE" ] && [ -n "$workdir" ]; then
        if [ -r "$workdir/cddbread.1" ]; then f="$workdir/cddbread.1"
        elif [ "$read_done" -gt 0 ] && [ -r "$workdir/cddbread.0" ]; then f="$workdir/cddbread.0"
        else f=""; fi
        [ -n "$f" ] && STATUS_TITLE=$(grep -m 1 '^DTITLE=' "$f" | cut -d= -f2- | tr -d '\r')
    fi
    current=$((read_done + 1)); [ "$current" -gt "$total" ] && current="$total"

    frames_done=0
    for ((i = 0; i < read_done; i++)); do frames_done=$((frames_done + ${TRACK_FRAMES[i]:-0})); done
    if [ "$read_done" -lt "$total" ] && [ -n "$workdir" ]; then
        wav=$(printf '%s/track%02d.wav' "$workdir" "$current")
        size=$(stat -c %s "$wav" 2>/dev/null); size="${size:-0}"
        i=$((size / 2352))   # ein CD-Sektor = 2352 Byte
        [ "$i" -gt "${TRACK_FRAMES[current-1]:-0}" ] && i="${TRACK_FRAMES[current-1]:-0}"
        frames_done=$((frames_done + i))
    fi
    percent=0
    [ "$TOTAL_FRAMES" -gt 0 ] && percent=$((frames_done * 100 / TOTAL_FRAMES))
    [ "$percent" -gt 99 ] && percent=99
    set_status ripping "$disc" "$total" "$current" "$percent"
}

# Jahr vor den Albumordner stellen ("1992 - Into The Web"), wenn das in der Weboberflaeche eingeschaltet ist
apply_year_prefix() {
    local marker="$1" disc="$2" first dir name year
    [ "$(tr -d '[:space:]' < "$STATE_DIR/year_prefix" 2>/dev/null)" = "1" ] || return 0
    first=$(rip_files "$marker" "$disc" | head -z -n 1 | tr -d '\0')
    [ -n "$first" ] || return 0
    dir=$(dirname "$first"); name=$(basename "$dir")
    year=$(flac_tag "$first" DATE); year="${year:0:4}"
    [[ "$year" =~ ^[0-9]{4}$ ]] || return 0             # ohne Jahr in den Tags bleibt der Name, wie er ist
    [[ "$name" =~ ^[0-9]{4}\ -\  ]] && return 0
    [ -e "$(dirname "$dir")/$year - $name" ] && return 0
    mv "$dir" "$(dirname "$dir")/$year - $name" && log "Albumordner heisst jetzt '$year - $name'."
}

# Laufenden Rip abbrechen: abcde samt Hilfsprogrammen beenden, angefangene Dateien verwerfen, CD auswerfen
cancel_rip() {
    local pid="$1" marker="$2" disc="$3" flac
    log "Abbruch angefordert, beende den Rip ..."
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    sleep 2
    kill -KILL -- "-$pid" 2>/dev/null
    for name in cdparanoia flac abcde; do
        for p in $(pidof "$name" 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
    done
    wait "$pid" 2>/dev/null
    rm -rf /tmp/abcde.* 2>/dev/null
    # Titel, die von dieser CD schon im Musikordner gelandet sind, wieder entfernen
    while IFS= read -r -d '' flac; do
        rm -f "$flac"
        rmdir "$(dirname "$flac")" 2>/dev/null                 # Albumordner, falls jetzt leer
        rmdir "$(dirname "$(dirname "$flac")")" 2>/dev/null    # Interpreten-Ordner, falls jetzt leer
    done < <(rip_files "$marker" "$disc")
    sleep 1
    eject_disc || log "Hinweis: Auswerfen nicht moeglich."
    log "Rip abgebrochen, angefangene Titel verworfen."
}

rm -f "$CANCEL_FILE" 2>/dev/null
set_status idle ""
trap 'set_status idle ""' EXIT

if [ ! -b "$DEVICE" ]; then
    log "FEHLER: $DEVICE ist im Container nicht vorhanden."
    log "Das Laufwerk wurde nicht durchgereicht (siehe 'devices:' in docker-compose.yml)."
    # Nicht sofort beenden, sonst startet Docker den Container in Dauerschleife neu
    sleep 60
    exit 1
fi

log "Warte auf Audio-CD in $DEVICE ..."
last_disc=""

while true; do
    # cd-discid liefert nur bei eingelegter Audio-CD ein Ergebnis
    if info=$(cd-discid "$DEVICE" 2>/dev/null); then
        disc_id="${info%% *}"

        if [ "$disc_id" != "$last_disc" ]; then
            log "Audio-CD erkannt (ID $disc_id), starte Rip ..."

            # cd-discid liefert: ID, Titelzahl, Startposition jedes Titels (in Sektoren), Gesamtlaenge in Sekunden
            read -r -a fields <<< "$info"
            track_total="${fields[1]:-0}"
            TRACK_FRAMES=(); TOTAL_FRAMES=0
            if [[ "$track_total" =~ ^[0-9]+$ ]] && [ "$track_total" -gt 0 ]; then
                end_frame=$(( ${fields[track_total+2]:-0} * 75 ))
                for ((t = 0; t < track_total; t++)); do
                    next="${fields[t+3]:-$end_frame}"; [ "$t" -eq $((track_total - 1)) ] && next="$end_frame"
                    len=$(( next - ${fields[t+2]:-0} )); [ "$len" -lt 0 ] && len=0
                    TRACK_FRAMES+=("$len"); TOTAL_FRAMES=$((TOTAL_FRAMES + len))
                done
            else
                track_total=0
            fi
            STATUS_DISC="$disc_id"
            STATUS_TITLE=""

            rm -f "$CANCEL_FILE" 2>/dev/null
            rm -rf /tmp/abcde.* 2>/dev/null     # Reste eines frueheren, abgebrochenen Rips
            set_status ripping "$disc_id" "$track_total" 1 0
            format=$(wanted_format)
            # Marker im Musikordner (gleiche Uhr wie die Rips), um die neuen Dateien wiederzufinden
            marker="$STATE_DIR/rip-start"
            touch "$marker"
            # Bei MP3 ist die FLAC-Datei nur ein Zwischenschritt: schnellste Stufe, ohne Pruefdurchlauf
            flacopts="-s -V -8"
            [ "$format" = "mp3" ] && flacopts="-s -0"

            # abcde laeuft im Hintergrund in einer eigenen Prozessgruppe, damit es sich samt
            # cdparanoia und flac abbrechen laesst; waehrenddessen wird der Fortschritt gemeldet
            RIP_FLACOPTS="$flacopts" setsid abcde -c /etc/abcde.conf -d "$DEVICE" -N &
            rip_pid=$!
            cancelled=false
            while kill -0 "$rip_pid" 2>/dev/null; do
                if [ -e "$CANCEL_FILE" ]; then
                    cancelled=true
                    cancel_rip "$rip_pid" "$marker" "$disc_id"
                    break
                fi
                [ "$track_total" -gt 0 ] && report_progress "$disc_id" "$track_total"
                sleep 2
            done

            if [ "$cancelled" = "true" ]; then
                rm -f "$CANCEL_FILE" "$marker" 2>/dev/null
                last_disc="$disc_id"    # falls das Auswerfen scheitert: dieselbe CD nicht gleich wieder rippen
                set_status idle ""
                log "Warte auf naechste Audio-CD ..."
                sleep "$POLL_INTERVAL"
                continue
            fi

            if wait "$rip_pid"; then
                log "Rip abgeschlossen."
            else
                log "FEHLER: Rip fehlgeschlagen (ID $disc_id)."
            fi
            set_status ripping "$disc_id" "$track_total" "$track_total" 100

            # Unbekannte CDs wuerden sich sonst gegenseitig ueberschreiben
            unknown="$OUTPUT/Unknown Artist/Unknown Album"
            if [ -d "$unknown" ]; then
                mv "$unknown" "$OUTPUT/Unknown Artist/Unknown Album [$disc_id]" \
                    && log "Keine Metadaten gefunden, gespeichert als 'Unknown Album [$disc_id]'."
            fi

            apply_year_prefix "$marker" "$disc_id"

            # Merken, damit dieselbe CD nicht erneut gerippt wird, solange sie im Laufwerk liegt
            last_disc="$disc_id"

            if [ "$EJECT" = "true" ]; then
                eject_disc || log "Hinweis: Auswerfen nicht moeglich."
            fi

            # MP3 entsteht aus den frisch gerippten FLAC-Dateien; die CD wird dafuer nicht mehr gebraucht
            if [ "$format" = "mp3" ]; then
                log "Wandle in MP3 um ..."
                convert_to_mp3 "$marker"
            fi
            rm -f "$marker" "$CANCEL_FILE" 2>/dev/null
            set_status idle ""

            log "Warte auf naechste Audio-CD ..."
        fi
    else
        # Laufwerk leer/offen oder keine Audio-CD
        last_disc=""
    fi

    sleep "$POLL_INTERVAL"
done
