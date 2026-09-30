# syntax=docker/dockerfile:1
#
# chipgraph-eda: chipgraph plus the open-source EDA toolchain (OSS CAD Suite)
# needed to run examples/tinysoc end to end (verilator, make, git, python3).
#
# Targets:
#   (default) - lean runtime: OSS CAD Suite + chipgraph CLI, no repo source, no dev deps.
#   test      - default plus chipgraph's dev dependency group (pytest, ...) and the
#               repo's tests/ and examples/ trees, for `pytest -m e2e tests/e2e` in CI.
#
# Build:
#   docker build -t chipgraph-eda .
#   docker build --target test -t chipgraph-eda:test .
#
# OSS CAD Suite is pinned by release date (a git tag on YosysHQ/oss-cad-suite-build,
# e.g. "2026-09-28") via OSS_CAD_SUITE_DATE, with a sha256 per architecture pinned
# separately since the project does not publish per-asset checksums. See docker/README.md
# for how to bump these.

ARG OSS_CAD_SUITE_DATE=2026-09-28
ARG OSS_CAD_SUITE_SHA256_AMD64=6d04cf3e36c45d4eacbe2fa07f3860b9a86f503c2b725bdee4d02e54defbf80d
ARG OSS_CAD_SUITE_SHA256_ARM64=7f80d70662b089363687663020d7f319c412e9ab0d243d8603634de763950802
ARG UV_VERSION=0.12.21
ARG PYTHON_VERSION=3.14

# --- uv: copy the static binary from astral's official image, pinned by tag + digest ---
FROM ghcr.io/astral-sh/uv:${UV_VERSION}@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

# --- eda-base: debian + the OSS CAD Suite release tarball for the build arch ------------
FROM debian:bookworm-slim AS eda-base
ARG OSS_CAD_SUITE_DATE
ARG OSS_CAD_SUITE_SHA256_AMD64
ARG OSS_CAD_SUITE_SHA256_ARM64
ARG TARGETARCH

# perl: verilator (from the suite) is itself a Perl script that needs core modules
# (e.g. FindBin) not in bookworm-slim's perl-base; make/git: what the tinysoc rules
# and chipgraph's `cmd` adapter shell out to; curl/xz-utils: only to fetch and unpack
# the suite tarball below, but there is no separate stage worth splitting them into.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils perl make git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) suite_arch=x64; sha256="${OSS_CAD_SUITE_SHA256_AMD64}" ;; \
        arm64) suite_arch=arm64; sha256="${OSS_CAD_SUITE_SHA256_ARM64}" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    compact_date="$(printf '%s' "${OSS_CAD_SUITE_DATE}" | tr -d '-')"; \
    file="oss-cad-suite-linux-${suite_arch}-${compact_date}.tgz"; \
    url="https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${OSS_CAD_SUITE_DATE}/${file}"; \
    curl -fsSL -o /tmp/oss-cad-suite.tgz "${url}"; \
    echo "${sha256}  /tmp/oss-cad-suite.tgz" | sha256sum -c -; \
    tar -xzf /tmp/oss-cad-suite.tgz -C /opt; \
    rm -f /tmp/oss-cad-suite.tgz

# --- chipgraph-base: Python (via uv) + chipgraph installed from repo source, no dev deps --
FROM eda-base AS chipgraph-base
ARG PYTHON_VERSION
COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy \
    PATH=/opt/oss-cad-suite/bin:/opt/chipgraph/.venv/bin:/usr/local/bin:${PATH}

RUN uv python install "${PYTHON_VERSION}"

WORKDIR /opt/chipgraph
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable --python "${PYTHON_VERSION}"

# --- test: chipgraph-base plus the dev dependency group and the repo's test trees -------
FROM chipgraph-base AS test
ARG PYTHON_VERSION

RUN uv sync --locked --no-editable --python "${PYTHON_VERSION}"

COPY tests ./tests
COPY examples ./examples

ENTRYPOINT []
CMD ["uv", "run", "--no-sync", "pytest", "-m", "e2e", "tests/e2e"]

# --- final (default): lean runtime, no repo source, no dev deps -------------------------
FROM debian:bookworm-slim AS final

LABEL org.opencontainers.image.source="https://github.com/quynhonsemiconductor/chipgraph" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="chipgraph CLI with a pinned OSS CAD Suite for RTL checks (verilator, yosys, ...)"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates perl make git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin chipgraph

# The venv's interpreter is a symlink into uv's managed Python install (not copied by
# `uv sync`), so that install has to come along too — this also gives us `python3` on
# PATH, which examples/tinysoc's `gen_manifest.py` rule invokes by that literal name.
COPY --from=eda-base /opt/oss-cad-suite /opt/oss-cad-suite
COPY --from=chipgraph-base /opt/uv/python /opt/uv/python
COPY --from=chipgraph-base /opt/chipgraph/.venv /opt/chipgraph/.venv

ENV PATH=/opt/oss-cad-suite/bin:/opt/chipgraph/.venv/bin:${PATH}

WORKDIR /work
USER chipgraph

ENTRYPOINT ["chipgraph"]
CMD ["--help"]
