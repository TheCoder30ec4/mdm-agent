# MDM Agent in Docker. Build context is the repo root (the MDM fork is the ./misterdownloadmanager submodule).
#   docker compose up -d --build     (see docker-compose.yml)

# Stage 1: the fork's own Linux headless recipe (misterdownloadmanager/Dockerfile). Slow: whole-program release build.
FROM rust:1-trixie AS mdm
RUN apt-get update \
 && apt-get install -y --no-install-recommends cmake libclang-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY misterdownloadmanager/ .
# The fork's release profile (fat LTO, one codegen unit) ran out of memory in a 7.6 GB Docker VM, so thin LTO here;
# `--build-arg MDM_LTO=fat --build-arg MDM_CGU=1` restores the fork's own profile (needs ~12+ GB for the VM).
ARG MDM_LTO=thin
ARG MDM_CGU=16
ENV CARGO_PROFILE_RELEASE_DEBUG=false CARGO_PROFILE_RELEASE_LTO=$MDM_LTO CARGO_PROFILE_RELEASE_CODEGEN_UNITS=$MDM_CGU
RUN set -e; \
    cargo build --locked --profile release -p mdm-app -p mdm-cli; \
    mkdir -p /out; cp target/release/misterdownloadmanager target/release/misterdownloadmanager-cli /out/; \
    strip /out/*; \
    rm -rf target /usr/local/cargo/registry /usr/local/cargo/git

# Stage 2: the app
FROM python:3.12-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# no .env in the image: compose passes it with env_file
COPY server.py ./
COPY agent/ agent/
COPY static/ static/
COPY contracts/ contracts/
COPY --from=mdm /out/ /usr/local/bin/
# /library: catalogue, files and posters (the volume). /opt/mdm: MDM's own folder, holding yt-dlp for video-get;
# installed here at build time (MDM verifies its checksums), outside the volume so a mount does not hide it.
RUN mkdir -p /library /opt/mdm && MDM_DATA_DIR=/opt/mdm misterdownloadmanager-cli install-ytdlp \
 && chown -R 1000:1000 /library /opt/mdm
ENV MDM_CLI=/usr/local/bin/misterdownloadmanager-cli LIBRARY_DIR=/library MDM_DATA_DIR=/opt/mdm PYTHONUNBUFFERED=1
USER 1000:1000
EXPOSE 8000
# --forwarded-allow-ips '*': behind Docker's port forward the peer is the bridge gateway, and the proxy (tailscale
# serve) is on the host; needed so https requests get the Secure cookie. Safe only because compose publishes
# 127.0.0.1:8000 and nothing else reaches this port.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
