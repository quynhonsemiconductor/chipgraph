# `chipgraph-eda` image (M0-17)

`ghcr.io/quynhonsemiconductor/chipgraph-eda` bundles the `chipgraph` CLI with a
pinned build of the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)
(Verilator, Yosys, nextpnr, ...) so the tinysoc example — and real projects — can be
linted and checked without installing an EDA toolchain by hand.

## Build

Base image: `debian:bookworm-slim`. Chosen over `ubuntu:24.04` because the OSS CAD
Suite's Linux releases are static-ish (bundled `lib/`, own Python, own OpenSSL) and
only need a small glibc + a handful of shared libraries at the base; bookworm-slim is
smaller than Ubuntu's base and gives the same glibc family the suite is built against.

Stages, in `docker/Dockerfile`:

- `uv` — the `uv`/`uvx` binaries, copied from `ghcr.io/astral-sh/uv:<version>`, pinned
  by tag **and** digest.
- `eda-base` — `debian:bookworm-slim` + the OSS CAD Suite release tarball for
  `$TARGETARCH` (`amd64` → `linux-x64`, `arm64` → `linux-arm64`), downloaded and
  checked against a per-arch sha256, plus what the `edalize` sim adapter needs from
  Debian: `g++`, `liblz4-dev`, `zlib1g-dev` and `iverilog`, the latter linked into
  `/opt/sim/bin`, which is first on `PATH` so Debian's Icarus wins over the suite's
  (the suite's `vvp` cannot load a pip-installed cocotb; `docs/spikes/S3.md`).
- `chipgraph-base` — installs Python (via `uv python install`) and `chipgraph` itself
  from the repo source (`uv sync --locked --no-dev --no-editable --extra sim`) into
  `/opt/chipgraph/.venv`.
- `test` — `chipgraph-base` plus the `dev` dependency group (pytest, ...) and the
  repo's `tests/` and `examples/` trees. Used only in CI / `make image-e2e`, never
  pushed.
- *(default, unnamed final stage)* — lean runtime: just the OSS CAD Suite, uv's
  managed Python (needed because the venv's interpreter is a symlink into it, not a
  copy), the `.venv`, the sim packages above, and `perl`/`make`/`git`/`ca-certificates`
  (`perl` is what Verilator's own launcher script needs; bookworm-slim's `perl-base`
  alone is not enough). No repo source, no dev deps, non-root user, `WORKDIR /work`,
  `ENTRYPOINT ["chipgraph"]`.

```bash
# lean runtime image (what gets pushed)
docker build -t chipgraph-eda -f docker/Dockerfile .
docker run --rm chipgraph-eda --version
docker run --rm --entrypoint verilator chipgraph-eda --version

# test image (adds pytest + tests/ + examples/), for the tinysoc e2e suite
docker build --target test -t chipgraph-eda:test -f docker/Dockerfile .
docker run --rm chipgraph-eda:test uv run --no-sync pytest -m e2e tests/e2e

# or, via the Makefile
make image
make image-e2e
```

## Pinning

| What | Where | How to update |
| --- | --- | --- |
| OSS CAD Suite release | `OSS_CAD_SUITE_DATE` build arg (a release tag on `YosysHQ/oss-cad-suite-build`, e.g. `2026-09-28`) | `gh release list -R YosysHQ/oss-cad-suite-build -L 5` for the latest tag |
| OSS CAD Suite tarball checksum | `OSS_CAD_SUITE_SHA256_AMD64` / `OSS_CAD_SUITE_SHA256_ARM64` build args | The project does not publish per-asset checksums, so download the `linux-x64`/`linux-arm64` tarball for the new date and compute `sha256sum` yourself, e.g. `curl -fsSL -o t.tgz https://github.com/YosysHQ/oss-cad-suite-build/releases/download/<date>/oss-cad-suite-linux-x64-<compact-date>.tgz && sha256sum t.tgz` |
| `uv` | the `FROM ghcr.io/astral-sh/uv:<version>@sha256:<digest>` line | `gh api repos/astral-sh/uv/releases/latest --jq .tag_name` for the version, then resolve its digest, e.g. via `crane digest ghcr.io/astral-sh/uv:<version>` or the registry API (`curl` the `/v2/astral-sh/uv/manifests/<version>` endpoint with an anonymous ghcr.io pull token) |
| Python | `PYTHON_VERSION` build arg | matches `pyproject.toml`'s `requires-python` |

## Image size

Measured locally (`docker images`, linux/arm64, OSS CAD Suite `2026-09-28`):

| Image | Size |
| --- | --- |
| `chipgraph-eda` (default) | 2.66 GB |
| `chipgraph-eda:test` | 2.98 GB |

The OSS CAD Suite is kept whole (not trimmed to just Verilator, ~2 GB uncompressed on
its own): it is a single tarball with internal `lib/`/`share/` dependencies between
its tools (Yosys, nextpnr, GHDL, ...), and picking it apart is not simple enough to be
worth the size savings at this stage.

## CI (`.github/workflows/image.yml`)

- On a PR (or push to `main`/a `v*` tag) touching `docker/**`, `pyproject.toml`,
  `uv.lock`, `src/**`, `tests/e2e/**`, `examples/**` or the workflow itself: build the
  `test` target (buildx + GHA cache, not pushed) and run
  `pytest -m e2e tests/e2e` inside it, then fail the job unless the junit report shows
  at least 5 passing tests and zero skipped (a skip there means `verilator`/`make`
  went missing from the image, which should never happen).
- On push to `main` or a `v*` tag: also build and push the default (lean) target to
  `ghcr.io/quynhonsemiconductor/chipgraph-eda`, tagged `main`, `sha-<short>`, and (for
  tags) the version — `packages: write` is scoped to that job only.
