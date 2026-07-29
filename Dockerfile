# syntax=docker/dockerfile:1.7
# ============================================================================
# GNC Invoice Automation — Backend Dockerfile
# Single-stage image, works for Render's Docker deploys and for local docker-compose.
# ============================================================================

FROM python:3.11-slim

# Prevents Python from writing .pyc files and buffers stdout (better logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   - libpq5      : needed by asyncpg / psycopg2 at runtime
#   - build-essential + libpq-dev : needed to compile psycopg2 during pip install
#   - tesseract-ocr + poppler-utils : needed later (Step 4) for OCR / PDF → image
# We keep build tools installed because Render's slim images are ephemeral;
# the size cost is small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libpq5 \
        curl \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better Docker layer caching).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the app source.
COPY . .

# Render sets $PORT dynamically; default to 8000 for local docker-compose.
ENV PORT=8000
EXPOSE 8000

# Uvicorn is the ASGI server. `--proxy-headers` respects X-Forwarded-For
# from Render's load balancer so we log real client IPs.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
