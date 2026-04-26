FROM python:3.12-slim

# Pillow needs a couple of native libs for JPEG/PNG/etc. recipe-scrapers
# pulls in lxml-html-clean which compiles fine without extras.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==21.2.0 Pillow

# Copy the rest of the app.
COPY . .

# Bake the generated assets (icons + alarm WAV) into the image.
RUN python generate_icons.py \
 && python generate_beep.py

# If the repo includes seed_data/ (a local snapshot of the DB and the
# uploaded photos), bake it into /seed/ so the entrypoint can copy it
# into the volumes the first time the container starts on a host that
# has empty volumes.
RUN if [ -d /app/seed_data ]; then mv /app/seed_data /seed; else mkdir -p /seed; fi

# Run as a non-root user.
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data /app/static/uploads \
 && chown -R app:app /app /data /seed
USER app

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]

ENV SHOPPINGLIST_DB=/data/shoppinglist.db \
    HOST=0.0.0.0 \
    PORT=80 \
    PYTHONUNBUFFERED=1

EXPOSE 80

# 2 workers is plenty for family LAN traffic; bump if you ever need more.
# Binding to port 80 as a non-root user requires the
# `net.ipv4.ip_unprivileged_port_start=0` sysctl, set in docker-compose.yml.
CMD ["gunicorn", "--bind", "0.0.0.0:80", \
     "--workers", "2", "--threads", "4", \
     "--access-logfile", "-", "app:app"]
