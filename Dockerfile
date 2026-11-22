FROM python:3.15-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY entrypoint.py healthcheck.py /
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PUID=99 PGID=100 UMASK=022 PORT=8789
VOLUME /data
EXPOSE 8789
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD ["python", "/healthcheck.py"]
LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/recordhound.png" \
      org.opencontainers.image.title="Recordhound" \
      org.opencontainers.image.description="Music recommendations from your own Plex library and listening history"
# Nightly builds pass which build they are; releases leave it empty
ARG APP_BUILD=""
ENV APP_BUILD=$APP_BUILD
ENTRYPOINT ["python", "/entrypoint.py"]
CMD ["python", "-m", "app.serve"]
