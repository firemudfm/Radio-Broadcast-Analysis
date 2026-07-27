# syntax=docker/dockerfile:1.7
#
# FastAPI control plane.
#
# Deliberately lean: no FFmpeg, no ASR stack, no models. The API serves
# campaigns and reads results; it never decodes audio, so shipping the decoder
# here would add hundreds of megabytes and a much larger attack surface to the
# one container that is actually exposed to the network.
#
# Multi-architecture: `python:3.11-slim-bookworm` publishes linux/amd64 and
# linux/arm64, so `docker buildx build --platform linux/amd64,linux/arm64`
# produces both from this file unchanged.

ARG PYTHON_IMAGE=python:3.11.14-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt ./
# Installed into a prefix so the runtime stage copies only the packages, not
# pip, its cache, or any build tooling.
RUN python -m pip install --prefix=/install --no-compile -r requirements.txt


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    RADIO_API_HOST=0.0.0.0 \
    RADIO_API_PORT=8788

# Security updates only; no new packages. Each RUN layer is squashed by the
# apt lists cleanup so no package index ends up in the image.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Fixed uid/gid so bind-mounted host directories have predictable ownership;
# an auto-assigned uid makes /var/lib/radio permissions a guessing game.
RUN groupadd --gid 10001 radio \
    && useradd --uid 10001 --gid radio --no-create-home --shell /usr/sbin/nologin radio

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=root:root app ./app
COPY --chown=root:root VERSION ./VERSION
COPY --chown=root:root docker/healthchecks ./healthchecks

# Application code is root-owned and read-only to the runtime user: a
# compromised process cannot rewrite the code it is running.
USER radio:radio

EXPOSE 8788

# Readiness rather than liveness: /readyz is cheap by design (SQLite plus one
# stat) and answers the question an orchestrator actually has.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/healthchecks/api.py"]

ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8788", "--no-server-header"]
