FROM node:24-trixie-slim@sha256:6950b66b4c0cb0151ce89fa75074673850763d096b044f422c6729b588dd4956 AS node_runtime
FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    FOOTBALL_YTDLP=/opt/venv/bin/yt-dlp \
    FOOTBALL_DATA_DIR=/data \
    FOOTBALL_VAD_MODEL=/opt/models/silero-vad-v6.1.onnx \
    HOST=0.0.0.0 \
    PORT=8476

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

WORKDIR /app
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/LICENSE /usr/local/share/doc/node/LICENSE
COPY requirements-linux.lock ./
RUN python -m pip install --no-cache-dir -r requirements-linux.lock
COPY . .
RUN python -m app.crowd_audio --setup-model \
    && python -m scripts.build_ui \
    && python -m scripts.doctor --model \
    && useradd --create-home --uid 10001 clips \
    && mkdir -p /data \
    && chown -R clips:clips /app /data

USER clips
EXPOSE 8476
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1',int(os.environ.get('PORT','8476'))),timeout=3).close()"
CMD ["python", "server.py"]
