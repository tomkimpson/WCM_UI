# syntax=docker/dockerfile:1.7
#
# wcEcoli worker image.
#
# Single-stage build. Ports the recipe from upstream's two-stage
# cloud/docker/runtime + cloud/docker/wholecell Dockerfiles. See
# docs/notes/wcecoli-build-notes.md for justification of every decision.

# NOTE: python:3.11.3-slim resolves to Debian bullseye-slim, which reaches LTS EOL
# 2026-08-31. After that, apt mirrors may stop serving updates — bump the base then.
FROM python:3.11.3-slim
# python:3.11.3-slim resolves to debian:bullseye-slim. Upstream uses bare
# python:3.11.3 (same Debian release). The -slim variant matches upstream's
# Debian version and gives us a smaller base; build-essential and friends
# are layered on by the apt-get below. There is no python:3.11.3-slim-bookworm
# tag on Docker Hub (verified 2026-05-22).

# System dependencies. Matches upstream cloud/docker/runtime/Dockerfile line 23
# (swig gfortran llvm cmake nano) minus `nano` (not needed in a worker image),
# plus build-essential + git which are not preinstalled on the -slim variant.
#
# We deliberately do NOT install libopenblas-dev or libglpk-dev — see
# wcecoli-build-notes.md ("System dependencies"): OpenBLAS ships in the
# numpy/scipy wheels and GLPK ships inside the swiglpk wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gfortran \
        llvm \
        cmake \
        swig \
        git \
    && rm -rf /var/lib/apt/lists/*

# Upstream pins WORKDIR /wcEcoli and PYTHONPATH=/wcEcoli — see build notes
# ("PYTHONPATH must be set to the repo root").
WORKDIR /wcEcoli

# Pip bootstrap. Upstream upgrades pip/setuptools/wheel in one line
# (cloud/docker/runtime/Dockerfile:79), then installs requirements.txt which
# re-pins setuptools==73.0.1. We pin setuptools==73.0.1 here UP FRONT because
# of a second constraint the build notes don't yet cover: the requirements.txt
# package `Equation==1.2.1` uses a legacy `ez_setup.py` that fails to build
# under modern (>=74) setuptools. Pinning setuptools to 73.0.1 before any
# install fixes Equation AND keeps Aesara happy.
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip install --no-cache-dir 'setuptools==73.0.1'

COPY vendor/wcEcoli/requirements.txt ./requirements.txt

# Two-phase numpy install: numpy must be present in the parent env before any
# source-built package that imports it at build time (e.g. stochastic-arrow).
# See build notes ("Python dependencies file") and upstream
# cloud/docker/runtime/Dockerfile lines 79-81.
#
# --no-build-isolation is required because:
#   (a) Equation==1.2.1 uses a legacy ez_setup.py that fails under modern
#       (>=74) setuptools — pip's isolated build env pulls the latest
#       setuptools, ignoring our 73.0.1 pin. Disabling isolation makes it use
#       the parent env's setuptools 73.0.1.
#   (b) stochastic-arrow==1.0.0 imports numpy in its setup.py; isolated
#       builds don't see the numpy we just installed in the parent.
#
# This means the parent env must already have pip, wheel, setuptools, and
# numpy installed — which it does, by construction.
RUN pip install --no-cache-dir numpy==1.26.3 \
    && pip install --no-cache-dir --no-build-isolation -r requirements.txt

# Copy the pinned wcEcoli source.
COPY vendor/wcEcoli/ ./

# Build Cython extensions. Mandatory — three .pyx files under wholecell/utils/
# must be compiled before any run. See build notes ("Gotchas": make clean
# compile is mandatory).
RUN make clean compile

# Writable cache dirs. Replicates upstream cloud/docker/wholecell/Dockerfile
# line 62 verbatim. /.aesara is Aesara's compile cache; /wcEcoli/cache is
# where ParCa writes. Both need to be world-writable so the container can
# run as a non-root uid/gid via `docker run --user`.
RUN umask 000 && mkdir -p /.aesara /wcEcoli/cache

ENV PYTHONPATH=/wcEcoli
# Mandatory per build notes: without OPENBLAS_NUM_THREADS=1, OpenBLAS produces
# slightly different numerical results across runs.
ENV OPENBLAS_NUM_THREADS=1

CMD ["python3", "-c", "import wholecell; print('wcEcoli importable')"]
