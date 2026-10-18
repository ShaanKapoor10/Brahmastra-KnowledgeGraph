# Brahmastra backend — FastAPI + the pipeline.
#
# Two decisions dominate the size and the cold-start time of this image:
#
#   1. CPU-ONLY TORCH. sentence-transformers pulls torch, and the default wheel
#      ships CUDA kernels — roughly 2.5 GB that a server without a GPU will
#      never execute. The CPU index below cuts the image by well over a
#      gigabyte with no behavioural difference: entity resolution runs on CPU
#      either way.
#
#   2. THE EMBEDDING MODEL IS BAKED IN. all-MiniLM-L6-v2 is ~88 MB and is
#      downloaded on first use. Left to runtime that download happens on every
#      cold start, on the request that triggered it, and fails outright in a
#      network-restricted environment. Downloading it at build time makes cold
#      starts fast and the container work offline.

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# build-essential is needed by a few wheels; it stays in this stage only.
# Not version-pinned (hadolint DL3008): Debian deletes superseded versions from
# its archive, so an exact pin breaks the build at the next security update.
# The base image is pinned by digest, which is what makes the build repeatable.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# uv installs from uv.lock: every package at its locked version, every file
# checked against its locked SHA-256 hash, and --frozen refuses to build if the
# lock is out of date with pyproject.toml. The image installs the bytes that
# were reviewed, or it does not build. torch comes ONLY from the CPU index
# (pyproject [tool.uv.sources]); nothing else may. Updating: CONTRIBUTING.md.
COPY --from=ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependencies before source: cached until the lock changes, which is what
# keeps ordinary code edits from reinstalling torch.
COPY pyproject.toml uv.lock README.md* ./
RUN uv sync --frozen --no-dev --no-install-project

COPY brahmastra/ ./brahmastra/
COPY main.py ./
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Absolute, and inside the image. A relative cache path once put the 88 MB
    # model in backend/backend/.cache; here a wrong value would silently
    # re-download on every start.
    BRAHMASTRA_CACHE=/opt/model-cache \
    HF_HOME=/opt/model-cache \
    # Defaults a deploy can override. The database lives on the mounted volume,
    # never in the image layer, or every redeploy would discard the graph.
    #
    # GRAPH_BACKEND is deliberately NOT set here. Baking `sqlite` in made the
    # image's own default contradict the deployed one, so anything running this
    # image without compose -- a `docker run`, a platform that ignores compose
    # -- silently got a single-file store instead of the engine. The store
    # factory already defaults to sqlite when it is unset, so leaving it out
    # loses nothing and stops the image asserting an answer it should not have.
    BRAHMASTRA_DB=/data/concept_graph.db \
    BRAHMASTRA_CHECKPOINT_DIR=/data/checkpoints \
    PORT=8001 \
    # Progress bars are not logs: the embedding library draws them on stderr
    # whenever the log level is INFO, and they break one-JSON-object-per-line.
    TQDM_DISABLE=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    TRANSFORMERS_VERBOSITY=error

COPY --from=builder /opt/venv /opt/venv

# Bake the embedding model. Done as its own layer so it is cached across code
# changes, and before dropping privileges so the cache is owned by root and
# read-only to the app.
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', cache_folder='/opt/model-cache')" \
 && chmod -R a+rX /opt/model-cache

# Run unprivileged. /data is the mount point for the volume; it is created and
# chowned here so the container still starts when nothing is mounted.
RUN groupadd --system --gid 10001 brahmastra \
 && useradd --system --create-home --uid 10001 --gid 10001 brahmastra \
 && mkdir -p /data /data/checkpoints \
 && chown -R brahmastra:brahmastra /data

WORKDIR /app
COPY --from=builder /build/brahmastra /app/brahmastra
COPY --from=builder /build/main.py /app/main.py
# uvicorn's own lines (startup, the supervisor of the workers) use the same JSON
# formatter as the app, so every line on stdout is one JSON object.
COPY log-config.json /app/log-config.json

# Numeric, so an orchestrator can prove the user is not root without reading
# /etc/passwd (Kubernetes runAsNonRoot requires exactly this).
USER 10001:10001
EXPOSE 8001

# Readiness, not liveness: this verifies the graph store actually answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8001')+'/health/ready',timeout=8).status==200 else 1)"]

# ONE worker by default, on purpose. With GRAPH_BACKEND=sqlite the database is
# a single file and concurrent writers produce "database is locked"; the
# pipeline lock is per store, not per process. Scale out only with
# GRAPH_BACKEND=neo4j, and raise WEB_CONCURRENCY there.
ENV WEB_CONCURRENCY=1
# --no-access-log: uvicorn's access log prints raw paths with query strings,
# and search text lives there; main.trace_and_log logs route templates instead.
# The production safety check runs first, as its own step, so an unsafe
# configuration ends the container with a readable list and exit code 78
# (EX_CONFIG) rather than a traceback from inside uvicorn's startup.
# PROMETHEUS_MULTIPROC_DIR is emptied first: each worker's metric files live
# there, and files left by a previous container would be counted again.
CMD ["sh", "-c", "python -m brahmastra.preflight --enforce && if [ -n \"$PROMETHEUS_MULTIPROC_DIR\" ]; then rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\"; fi && exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001} --workers ${WEB_CONCURRENCY:-1} --no-access-log --log-config /app/log-config.json"]
