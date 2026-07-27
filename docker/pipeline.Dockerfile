# syntax=docker/dockerfile:1.7
#
# Pipeline workers: planner, listener, transcription, analysis, cleanup.
#
# One image for all five rather than five images. They share the whole service
# layer, and five near-identical images would multiply build time, registry
# size and patch surface for no isolation benefit -- they already run as
# separate containers with separate resource limits.
#
# FFmpeg comes from Debian bookworm's own repository (trusted source, security
# updates via the distribution) rather than a third-party static build.
#
# No model files. Models are 0.5-1.1 GB, licensed separately, and mounted
# read-only from the host at /models (see docs/MODEL_MANAGEMENT.md). Baking one
# in would put a licensed binary in every registry layer.

ARG PYTHON_IMAGE=python:3.11.14-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt requirements-pipeline.txt ./
# --only-binary=:all: is a guard, not a preference. Every pinned version has a
# verified cp311 aarch64 wheel; if one ever stops being published, this fails
# the build loudly instead of starting a silent, hours-long source compile.
RUN python -m pip install --prefix=/install --no-compile \
        --only-binary=:all: -r requirements-pipeline.txt


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    RADIO_MODEL_PATH=/models \
    RADIO_SPOOL_PATH=/var/lib/radio/spool \
    # Keep the numeric libraries single-threaded. CTranslate2 is given an
    # explicit thread count via RADIO_ASR_CPU_THREADS; letting OpenMP also fan
    # out per core oversubscribes 4 vCPUs and makes latency worse, not better.
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    # Never fetch a model implicitly at runtime.
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 radio \
    && useradd --uid 10001 --gid radio --no-create-home --shell /usr/sbin/nologin radio

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=root:root app ./app
COPY --chown=root:root VERSION ./VERSION
COPY --chown=root:root docker/entrypoints ./entrypoints
COPY --chown=root:root docker/healthchecks ./healthchecks
RUN chmod 0755 /app/entrypoints/*.sh

# Mount points, owned by the runtime user so a read-only root filesystem still
# leaves the writable paths writable.
RUN mkdir -p /var/lib/radio/spool /var/lib/radio/evidence /var/lib/radio/logs /models \
    && chown -R radio:radio /var/lib/radio \
    && chmod 0700 /var/lib/radio/spool

USER radio:radio

# No default worker: the role is always explicit, because a container that
# silently defaults to some role is a container that runs the wrong one.
ENTRYPOINT ["/app/entrypoints/worker.sh"]
