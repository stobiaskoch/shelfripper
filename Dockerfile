FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      abcde \
      cdparanoia \
      flac \
      lame \
      cd-discid \
      eject \
      libmusicbrainz-discid-perl \
      libwebservice-musicbrainz-perl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY abcde.conf /etc/abcde.conf
COPY rip.sh /usr/local/bin/rip.sh

# Falls die Dateien unter Windows mit CRLF gespeichert wurden: bereinigen
RUN sed -i 's/\r$//' /etc/abcde.conf /usr/local/bin/rip.sh \
 && chmod +x /usr/local/bin/rip.sh

VOLUME /output

ENTRYPOINT ["/usr/local/bin/rip.sh"]
