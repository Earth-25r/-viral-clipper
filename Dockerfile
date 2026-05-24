# ─────────────────────────────────────────────
#  Viral Clipper — Production Docker Image
#  Works on Railway, Render, Fly.io, VPS
# ─────────────────────────────────────────────
FROM python:3.11-slim

# System deps: FFmpeg + yt-dlp runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 clipper
WORKDIR /app

# Install Python deps first (layer cache)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY backend/ .

# Create work dirs
RUN mkdir -p /tmp/viral-clipper/outputs && \
    chown -R clipper:clipper /tmp/viral-clipper /app

USER clipper

EXPOSE 8000

# Railway injects $PORT; fallback to 8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2
