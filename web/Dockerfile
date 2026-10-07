FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-flask \
      python3-mutagen \
      python3-pil \
      python3-requests \
      python3-waitress \
      ca-certificates \
 && (apt-get install -y --no-install-recommends python3-smbprotocol \
     || (apt-get install -y --no-install-recommends python3-pip \
         && pip3 install --break-system-packages smbprotocol) \
     || echo "HINWEIS: smbprotocol nicht installierbar - Archiv auf Netzwerkfreigaben steht nicht zur Verfuegung") \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static

# Falls die Dateien unter Windows mit CRLF gespeichert wurden: bereinigen
RUN sed -i 's/\r$//' /app/app.py

VOLUME ["/output", "/config"]
EXPOSE 8080

CMD ["python3", "/app/app.py"]
