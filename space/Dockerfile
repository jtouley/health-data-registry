# The hosted, read-only MCP server on a Hugging Face Docker Space (CPU Basic hardware).
# Build context: a checkout of this repository at a tagged commit, with this file as the Space's Dockerfile and
# space/README.md and space/PRIVACY.md at the Space's root (docs/dev/hosting.md has the steps). Nothing secret is
# read at build time; the private-tier token (HDR_PRIVATE_HF_TOKEN) is a Space secret, read from the environment
# at run time only.
FROM python:3.12-slim

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
    DO_NOT_TRACK=1
WORKDIR /home/user/app

# Only what the server needs: the package, the reviewed registry and the lock file. Never a lake.
COPY --chown=user pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY --chown=user src ./src
COPY --chown=user catalog ./catalog
COPY --chown=user contracts ./contracts
COPY --chown=user semantic ./semantic
COPY --chown=user licenses ./licenses
RUN uv sync --frozen --no-dev --no-editable

# app_port in README.md. The cache goes to HDR_CACHE_DIR, else /data when storage is attached there, else /tmp.
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4)"
CMD ["health-data-registry", "serve", "--http", "--host", "0.0.0.0", "--port", "7860"]
