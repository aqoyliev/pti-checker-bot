# Builds the telegram-bot-api binary once and publishes it as a tiny artifact
# image to GHCR. The main Dockerfile consumes it via
#   COPY --from=ghcr.io/<owner>/telegram-bot-api:<commit> /telegram-bot-api ...
# so deploys never recompile tdlib. Built/pushed by
# .github/workflows/telegram-bot-api.yml — not used at deploy time directly.
#
# Built on debian:bookworm-slim to match the binary's glibc to the runtime base
# (python:3.11-slim). Newer-glibc runtimes run this older-linked binary fine.
FROM debian:bookworm-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake gperf zlib1g-dev libssl-dev git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG TGAPI_COMMIT
RUN test -n "$TGAPI_COMMIT" || (echo "TGAPI_COMMIT build-arg is required" && exit 1)
RUN git clone https://github.com/tdlib/telegram-bot-api.git /src \
    && cd /src && git checkout ${TGAPI_COMMIT} \
    && git submodule update --init --recursive

WORKDIR /src/build
RUN cmake -DCMAKE_BUILD_TYPE=Release .. && \
    cmake --build . -j"$(nproc)"

# Artifact-only image: carries just the binary for COPY --from.
FROM scratch
COPY --from=build /src/build/telegram-bot-api /telegram-bot-api
