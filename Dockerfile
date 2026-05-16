# syntax=docker/dockerfile:1
# Programa Core — Flask + PostgreSQL app para la fábrica Intela.
#
# Multi-stage para que la imagen final no arrastre cabeceras de compilación.

# -----------------------------------------------------------------------------
# Stage 1 — builder
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq para psycopg2, build-essential por si alguna dep compila
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2 — runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Flask/Waitress defaults — override via compose / ECS task def
    ENV=production \
    PORT=5050

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash app

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=app:app . /app
USER app

EXPOSE 5050

# Healthcheck — liveness del app. /healthz es el endpoint ligero sin DB;
# existe /healthz/ready que también verifica la DB, pero acá usamos el simple
# para no trigger-ear restarts cuando la DB está transient.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl -fsS http://localhost:5050/healthz || exit 1

# Waitress como WSGI server — mismo stack que la EC2 Windows para paridad.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5050", "--threads=8", "app:create_app"]
