# The hosted, read-only MCP server on a Hugging Face Docker Space (CPU Basic hardware).
# The Space repo holds only space/ (this Dockerfile, README.md with the Space's config header, PRIVACY.md);
# .github/workflows/deploy-space.yml syncs space/ to the Space exactly. The package source comes from one pinned
# commit of the public GitHub repository, so a deploy is reproducible: change HDR_COMMIT to deploy another
# commit (docs/dev/hosting.md has the steps). Nothing secret is read at build time; the private-tier token
# (HDR_PRIVATE_HF_TOKEN) is a Space secret, read from the environment at run time only.
ARG HDR_REPO=https://github.com/jtouley/health-data-registry.git
ARG HDR_COMMIT=3c448007f7cb4417b8342e12243933fb8f185993

# Stage 1: fetch exactly that commit (git checks every object against its hash) and verify it.
FROM python:3.12-slim AS source
ARG HDR_REPO
ARG HDR_COMMIT
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
    && git remote add origin "$HDR_REPO" \
    && git fetch -q --depth 1 origin "$HDR_COMMIT" \
    && git checkout -q --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$HDR_COMMIT"

# Stage 2: the server, without git.
FROM python:3.12-slim
ARG HDR_COMMIT

# uv, pinned to the release the maintainers use.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

# Spaces run the container as user id 1000.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    HDR_SOURCE_COMMIT=$HDR_COMMIT
WORKDIR /home/user/app

# Only what the server needs: the package, the reviewed registry and the lock file. Never a lake.
COPY --from=source --chown=user /src/pyproject.toml /src/uv.lock /src/README.md /src/LICENSE /src/NOTICE ./
COPY --from=source --chown=user /src/src ./src
COPY --from=source --chown=user /src/catalog ./catalog
COPY --from=source --chown=user /src/contracts ./contracts
COPY --from=source --chown=user /src/semantic ./semantic
COPY --from=source --chown=user /src/licenses ./licenses
RUN uv sync --frozen --no-dev --no-editable

# app_port in README.md. The cache goes to HDR_CACHE_DIR, else /data when storage is attached there, else /tmp.
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4)"
CMD ["health-data-registry", "serve", "--http", "--host", "0.0.0.0", "--port", "7860"]
