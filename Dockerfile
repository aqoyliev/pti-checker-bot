# telegram-bot-api is prebuilt and published to GHCR by
# .github/workflows/telegram-bot-api.yml (see tgapi.Dockerfile), so this build
# pulls the binary instead of recompiling tdlib — fast and cache-independent.
# This ARG is the single source of truth for the pinned commit; the workflow
# reads it from here. Bumping it triggers a rebuild of the GHCR image.
ARG TGAPI_COMMIT=01a3679c0bbf9bbba03d1d3e20f621fa4becddcc
FROM ghcr.io/aqoyliev/telegram-bot-api:${TGAPI_COMMIT} AS tgapi

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tgapi /telegram-bot-api /usr/local/bin/telegram-bot-api

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
