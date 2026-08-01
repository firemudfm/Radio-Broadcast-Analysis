# syntax=docker/dockerfile:1.7
#
# llama.cpp `llama-server` for the local Qwen3-0.6B analysis model.
#
# Built from a pinned source tag rather than pulled from a prebuilt image:
# the upstream project publishes releases faster than we can validate them,
# and an unpinned runtime would change decoding behaviour under us between
# deploys. The tag is verified in docs/research/TECHNOLOGY_RESEARCH.md §1.7.
#
# Multi-architecture from one file: the build compiles for the *build
# platform's* native architecture, so buildx produces a genuine aarch64 binary
# on an aarch64 builder and an x86-64 one on x86-64. No cross-compilation and
# no QEMU-emulated CPU feature detection.
#
# No model file. The GGUF is ~610 MiB, Apache-2.0 licensed separately, and is
# mounted read-only at /models.

ARG LLAMA_CPP_TAG=b10144
ARG BASE_IMAGE=debian:bookworm-20250811-slim

FROM ${BASE_IMAGE} AS builder

ARG LLAMA_CPP_TAG

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# --depth 1 on an exact tag: we want reproducibility, not history.
RUN git clone --depth 1 --branch "${LLAMA_CPP_TAG}" \
        https://github.com/ggml-org/llama.cpp.git . \
    && git rev-parse HEAD > /src/COMMIT

# CPU only. GGML_NATIVE=OFF matters: -march=native would bake in the *builder's*
# CPU features, and the resulting binary would crash with SIGILL on any host
# with a narrower feature set. Portability beats a few percent of throughput.
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_SERVER=ON \
        -DBUILD_SHARED_LIBS=ON \
    && cmake --build build --config Release --target llama-server -j "$(nproc)" \
    && mkdir -p /out/bin /out/lib \
    && cp build/bin/llama-server /out/bin/ \
    && find build -name '*.so*' -exec cp -a {} /out/lib/ \;


FROM ${BASE_IMAGE} AS runtime

ARG LLAMA_CPP_TAG
LABEL org.opencontainers.image.title="radio-llm" \
      org.opencontainers.image.description="llama-server for local Qwen3-0.6B analysis" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ggml-org/llama.cpp" \
      org.opencontainers.image.version="${LLAMA_CPP_TAG}"

# curl is here purely for HEALTHCHECK: this image has no Python interpreter,
# and adding one for a liveness probe would be a far larger dependency than a
# ~250 KB HTTP client.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libgomp1 curl \
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

COPY --from=builder /out/bin/llama-server /usr/local/bin/llama-server
COPY --from=builder /out/lib/ /usr/local/lib/
COPY --from=builder /src/COMMIT /etc/llama-cpp-commit
RUN ldconfig

COPY --chown=root:root docker/entrypoints/llm.sh /usr/local/bin/llm-entrypoint.sh
RUN chmod 0755 /usr/local/bin/llm-entrypoint.sh

ENV RADIO_LLM_MODEL_PATH=/models/qwen/Qwen3-0.6B-Q8_0.gguf \
    RADIO_LLM_PORT=8790 \
    RADIO_LLM_THREADS=2 \
    RADIO_LLM_CONTEXT=4096

USER radio:radio

# Documented for the Compose network only. compose.yaml deliberately does not
# publish this port: an unauthenticated inference endpoint must not be
# reachable from the host, let alone the internet.
EXPOSE 8790

# start-period is generous: loading a 610 MiB GGUF on 4 CPU cores takes tens of
# seconds, and a probe that fires earlier would restart-loop a healthy server.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${RADIO_LLM_PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/llm-entrypoint.sh"]
