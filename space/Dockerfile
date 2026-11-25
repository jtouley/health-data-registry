# The hosted, read-only MCP server on a Hugging Face Docker Space (CPU Basic hardware).
# The Space repo holds space/ (this Dockerfile, README.md with the Space's config header, PRIVACY.md) plus two files
# of one GitHub release, added by .github/workflows/deploy-space.yml: the signed wheel
# health_data_registry-X.Y.Z-py3-none-any.whl and the signed, hashed runtime requirements
# health-data-registry-X.Y.Z-requirements.txt that release.yml built from uv.lock. The deploy verifies both Sigstore
# signatures (built by release.yml at that tag) and that the wheel is the one on PyPI, then writes their sha256 here.
# Nothing is built from source: the Space runs exactly the artifact that was released.
#
# HDR_TAG, HDR_COMMIT and the two sha256 values are empty in git on purpose: deploy-space.yml fills them in from the
# release it deploys, and a build without them, or with files whose hashes differ, stops. Never fill them by hand
# for a branch or an unreleased commit. Nothing secret is read at build time; the private-tier token
# (HDR_PRIVATE_HF_TOKEN) is a Space secret, read from the environment at run time only.
ARG HDR_TAG=
ARG HDR_COMMIT=
ARG HDR_WHEEL_SHA256=
ARG HDR_REQUIREMENTS_SHA256=

# The base image is pinned by digest (the tag in front is for people), so a re-pushed tag can't change the build;
# Dependabot (docker ecosystem, /space) bumps the digests.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
ARG HDR_TAG
ARG HDR_COMMIT
ARG HDR_WHEEL_SHA256
ARG HDR_REQUIREMENTS_SHA256

# uv, pinned to the release the maintainers use.
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /usr/local/bin/uv

# Spaces run the container as user id 1000.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    HDR_SOURCE_TAG=$HDR_TAG \
    HDR_SOURCE_COMMIT=$HDR_COMMIT
WORKDIR /home/user/app

# The released wheel (it carries the reviewed catalog, contracts and semantic models) and the locked dependencies,
# each checked against the sha256 the deploy verified. Every dependency must match a hash from uv.lock. Never a lake.
COPY --chown=user health_data_registry-*-py3-none-any.whl health-data-registry-*-requirements.txt ./
RUN case "$HDR_TAG" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo "HDR_TAG must be a release tag vX.Y.Z" >&2; exit 1 ;; esac \
    && test -n "$HDR_COMMIT" && test -n "$HDR_WHEEL_SHA256" && test -n "$HDR_REQUIREMENTS_SHA256" \
    && version="${HDR_TAG#v}" \
    && wheel="health_data_registry-${version}-py3-none-any.whl" \
    && requirements="health-data-registry-${version}-requirements.txt" \
    && printf '%s  %s\n%s  %s\n' "$HDR_WHEEL_SHA256" "$wheel" "$HDR_REQUIREMENTS_SHA256" "$requirements" \
        | sha256sum --strict -c - \
    && uv venv --python /usr/local/bin/python3 .venv \
    && uv pip install --python .venv/bin/python --require-hashes --no-deps -r "$requirements" \
    && uv pip install --python .venv/bin/python --no-deps "./$wheel" \
    && rm -- ./*.whl ./*-requirements.txt

# app_port in README.md. The cache goes to HDR_CACHE_DIR, else /data when storage is attached there, else /tmp.
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=4)"
CMD ["health-data-registry", "serve", "--http", "--host", "0.0.0.0", "--port", "7860"]
