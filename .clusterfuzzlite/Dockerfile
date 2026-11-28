# ClusterFuzzLite build image: OSS-Fuzz's Python builder (Atheris and PyInstaller included), pinned by digest.
# Dependabot (docker ecosystem, /.clusterfuzzlite) bumps the digest. The image runs Python 3.11; see build.sh.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:5a20f6927f4157bafbb9f66eb62a99dd9e6e92ed94b10ffaee4262b9bedac08a
COPY . $SRC/health-data-registry
WORKDIR $SRC/health-data-registry
COPY .clusterfuzzlite/build.sh $SRC/
