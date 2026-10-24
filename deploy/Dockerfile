# media-library console + tools. Python + three pinned, hash-checked
# dependencies (psycopg, psycopg-pool, Pillow; cryptography for passkeys).
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock
COPY medialib /app/medialib
COPY migrations /app/migrations
COPY web /app/web
USER 1000:1000
EXPOSE 8099
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4)"]
CMD ["python", "-m", "medialib", "serve", "--host", "0.0.0.0", "--port", "8099"]
