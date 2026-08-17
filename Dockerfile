FROM python:3.12-slim-bookworm

ARG BUILD_ID=medialab-0.4.1-20260817-r1
LABEL org.opencontainers.image.title="MediaLab" \
      org.opencontainers.image.version="0.4.1" \
      org.opencontainers.image.revision="${BUILD_ID}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PORT=8099 \
    MEDIA_ROOT=/media \
    DATA_ROOT=/data \
    BUILD_ID=${BUILD_ID}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ADD medialab-0.4.1-build.tar.gz /app/
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt \
    && cp /app/entrypoint.sh /usr/local/bin/medialab-entrypoint \
    && chmod 0755 /usr/local/bin/medialab-entrypoint \
    && mkdir -p /data /media \
    && ffprobe -version >/dev/null \
    && python -c "from app import __version__; assert __version__ == '0.4.1', __version__"

EXPOSE 8099
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').getenv('APP_PORT','8099') + '/health', timeout=3)" || exit 1
ENTRYPOINT ["/usr/local/bin/medialab-entrypoint"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-8099}"]
