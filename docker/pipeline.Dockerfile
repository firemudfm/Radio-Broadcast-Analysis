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

# Runtime identity is a BUILD ARGUMENT, not a constant. See docker/api.Dockerfile
# for the full rationale: the production host's `radio` account is uid 992, not
# 10001, and bind mounts must be writable without a recursive chown.
ARG RADIO_UID=10001
ARG RADIO_GID=10001

RUN set -eu; \
    for value in "${RADIO_UID}" "${RADIO_GID}"; do \
        case "${value}" in \
            ''|*[!0-9]*) echo "RADIO_UID/RADIO_GID must be numeric, got '${value}'" >&2; exit 1 ;; \
        esac; \
        [ "${value}" -ge 1 ] || { echo "RADIO_UID/RADIO_GID must not be 0 (root)" >&2; exit 1; }; \
        [ "${value}" -le 65533 ] || { echo "RADIO_UID/RADIO_GID above 65533 is reserved" >&2; exit 1; }; \
    done; \
    groupadd --gid "${RADIO_GID}" radio; \
    useradd --uid "${RADIO_UID}" --gid "${RADIO_GID}" \
        --no-create-home --shell /usr/sbin/nologin radio

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
