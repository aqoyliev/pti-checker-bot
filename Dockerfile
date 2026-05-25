FROM debian:bookworm-slim AS tg-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake gperf zlib1g-dev libssl-dev git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG TGAPI_COMMIT=01a3679c0bbf9bbba03d1d3e20f621fa4becddcc
RUN git clone https://github.com/tdlib/telegram-bot-api.git /src \
    && cd /src && git checkout ${TGAPI_COMMIT} \
    && git submodule update --init --recursive

WORKDIR /src/build
RUN cmake -DCMAKE_BUILD_TYPE=Release .. && \
    cmake --build . -j2

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tg-builder /src/build/telegram-bot-api /usr/local/bin/telegram-bot-api

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
