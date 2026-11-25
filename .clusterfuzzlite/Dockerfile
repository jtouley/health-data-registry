# ClusterFuzzLite build image: OSS-Fuzz's Python builder (Atheris and PyInstaller included), pinned by digest.
# Dependabot (docker ecosystem, /.clusterfuzzlite) bumps the digest. The image runs Python 3.11; see build.sh.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:98449306b5efefb0275029a5f09302f863f6a30ab94c5b1c217cea3836d6904f
COPY . $SRC/health-data-registry
WORKDIR $SRC/health-data-registry
COPY .clusterfuzzlite/build.sh $SRC/
