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

# Runtime identity is a BUILD ARGUMENT, not a constant.
#
# Bind-mounted host directories must be writable by whatever uid the container
# runs as, and the host's `radio` account is not guaranteed to be 10001 -- the
# production host uses 992. Baking 10001 in forced either a recursive chown of
# /var/lib/radio or a world-writable spool, and both are worse than rebuilding
# the image with the host's real ids.
#
# Defaults stay 10001 so local development and generic builds are unchanged.
ARG RADIO_UID=10001
ARG RADIO_GID=10001

RUN set -eu; \
    for value in "${RADIO_UID}" "${RADIO_GID}"; do \
        case "${value}" in \
            ''|*[!0-9]*) echo "RADIO_UID/RADIO_GID must be numeric, got '${value}'" >&2; exit 1 ;; \
        esac; \
        # 0 is root: the whole point is that the runtime user is unprivileged.
        [ "${value}" -ge 1 ] || { echo "RADIO_UID/RADIO_GID must not be 0 (root)" >&2; exit 1; }; \
        # 65534 is `nobody` on Debian; 65535 is reserved.
        [ "${value}" -le 65533 ] || { echo "RADIO_UID/RADIO_GID above 65533 is reserved" >&2; exit 1; }; \
    done; \
    groupadd --gid "${RADIO_GID}" radio; \
    useradd --uid "${RADIO_UID}" --gid "${RADIO_GID}" \
        --no-create-home --shell /usr/sbin/nologin radio

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
