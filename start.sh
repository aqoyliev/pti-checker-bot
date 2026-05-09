#!/bin/sh
set -e

mkdir -p /var/lib/telegram-bot-api

telegram-bot-api \
  --local \
  --dir=/var/lib/telegram-bot-api \
  --api-id="${TELEGRAM_API_ID}" \
  --api-hash="${TELEGRAM_API_HASH}" \
  --http-port=8081 &

# Wait for local server to be ready (up to 30s)
python3 -c "
import urllib.request, urllib.error, time, sys
for _ in range(30):
    try:
        urllib.request.urlopen('http://localhost:8081', timeout=2)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(1)
"

exec python app.py
