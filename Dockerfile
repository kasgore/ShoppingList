FROM python:3.12-slim

# Pillow needs a couple of native libs for JPEG/PNG/etc. recipe-scrapers
# pulls in lxml-html-clean which compiles fine without extras.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        ca-certificates \
        tesseract-ocr \
        tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==21.2.0 Pillow

# Bake the MiniLM embedding model into the image so the container doesn't
# have to download ~22 MB on first request. Cache lives at /app/.fastembed
# so it gets chowned to the `app` user along with the rest of /app.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed
RUN mkdir -p /app/.fastembed \
 && python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2'); print('MiniLM cached')" \
 || echo "MiniLM cache failed — semantic search will degrade to no-op at runtime"

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

# Single worker + threads so SSE subscribers share one in-memory queue
# (multi-worker would only fan out within the worker that received the
# write). 8 threads handle the SSE long-poll connections plus normal
# request traffic comfortably for family-scale use.
# Binding to port 80 as a non-root user requires the
# `net.ipv4.ip_unprivileged_port_start=0` sysctl, set in docker-compose.yml.
CMD ["gunicorn", "--bind", "0.0.0.0:80", \
     "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "app:app"]
