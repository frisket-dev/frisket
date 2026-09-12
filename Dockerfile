# frisket public single-organization team image
# Stage 1: frontend build
FROM node:22-slim AS web
# web/package.json wraps builds through ../scripts/e2e/checklog.py; with
# WORKDIR /build, that resolves to /scripts/e2e/checklog.py in this stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python-is-python3 \
    && rm -rf /var/lib/apt/lists/*
# The web lock resolves @frisket/plugin-sdk from the repository sibling at
# file:../sdk. Build that package before npm snapshots it into web/node_modules.
# Keep dependency metadata ahead of source so SDK edits retain the npm-ci layer.
WORKDIR /sdk
COPY sdk/package*.json ./
RUN npm ci
COPY sdk/ ./
RUN npm run build
WORKDIR /build
COPY web/package*.json ./
RUN npm ci
COPY scripts/e2e/checklog.py /scripts/e2e/checklog.py
# Frontend catalogs and shared action UI types import backend-owned assets via
# ../src/..., which resolves to /src/... in this build stage.
COPY src/frisket/data/ /src/frisket/data/
COPY web/ ./
# Keep the wrapper path exercised without writing disposable audit files into
# Docker build-cache layers.
ENV FRISKET_CHECKLOG_DISABLE=1
RUN npm run build

# Stage 2: python app + media toolbelt
FROM python:3.12-slim
ARG EXIFTOOL_VERSION=13.59
ARG EXIFTOOL_SHA256=668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config libicu-dev ffmpeg poppler-utils curl perl \
    libgl1 libglib2.0-0 libxcb1 \
    && rm -rf /var/lib/apt/lists/*
# ExifTool is part of the supported runtime, not an undeclared host
# dependency.  Pin both the upstream release and its published SHA-256 so a
# rebuild cannot silently pick up different probe code.  The official project
# currently serves release archives from SourceForge.
RUN curl --fail --show-error --silent --location \
      --output /tmp/exiftool.tar.gz \
      "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download" \
    && printf '%s  %s\n' "$EXIFTOOL_SHA256" /tmp/exiftool.tar.gz \
      | sha256sum --check --strict - \
    && mkdir --parents /tmp/exiftool-src \
    && tar --extract --gzip --file /tmp/exiftool.tar.gz \
      --directory /tmp/exiftool-src --strip-components=1 \
    && cd /tmp/exiftool-src \
    && perl Makefile.PL \
    && make install \
    && test "$(exiftool -ver)" = "$EXIFTOOL_VERSION" \
    && mkdir --parents /usr/local/share/doc/exiftool \
    && install --mode=0644 README /usr/local/share/doc/exiftool/ \
    && rm -rf /tmp/exiftool-src /tmp/exiftool.tar.gz
COPY --from=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv
# yt-dlp's YouTube challenge solver requires an external JavaScript runtime;
# Deno is its recommended, default-enabled runtime.
COPY --from=denoland/deno:bin-2.9.3 /deno /usr/local/bin/deno
RUN deno --version
WORKDIR /app
COPY pyproject.toml uv.lock LICENSE ./
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md
# pyproject.toml declares readme = "README.md", so uv needs the file to exist
# to parse project metadata — but with --no-install-project its content is
# never packaged. A stub keeps README edits from invalidating the expensive
# dependency/model layers; the real README lands with the app layer below.
RUN touch README.md
# One owned copy of the image's extra set; both sync lines below must install
# the identical extras or the second sync silently changes the environment the
# model warm-up above ran in.
ARG FRISKET_UV_EXTRAS="--extra standard --extra entities"
# standard is in the image set because spaCy is the default NER engine:
# without it the out-of-box "Extract named entities" action fails on an
# install hint instead of reaching the managed, confirmed model download.
# libicu-dev above is here FOR entities (pyicu builds against it); keep it in
# the image's extra set so the team-server product doesn't silently lose
# FollowTheMoney/graph-neighborhood support now that it's opt-in.
# pdf is in the image set because the UI's PDF Tables action
# (media.extract_pdf_tables) is registered unconditionally, same as every
# other recipe here -- shipping the action without the extra meant the team
# image showed it and then failed on an install hint. natural-pdf (the
# extra's one dependency) reuses onnxruntime/numpy/huggingface-hub already in
# this image via standard/base, but does add its own real weight: pandas, scipy,
# scikit-learn, scikit-image, and matplotlib are all new. Accepted here
# because the action was already unconditionally visible in the UI before
# this fix -- the choice was never "smaller image," only "the visible action
# works" versus "it silently can't."
# --no-install-project: the dependency set is fully pinned by uv.lock, so it
# resolves without src/ present. Keeping src/ out of this layer's build
# context means editing application code cannot invalidate it.
RUN uv sync --frozen --no-dev --no-install-project $FRISKET_UV_EXTRAS
# Pre-download the small local models so the box works offline at runtime —
# cell text never has to leave it. Embeddings are ~0.2GB and RapidOCR ~15MB.
# Hugging Face-backed local ASR models stay cold until first use, then reuse
# the persistent repository cache under /data configured below.
# Model ids are inlined rather than imported from frisket.semantic /
# frisket.search: importing frisket would require src/ in this layer,
# defeating the point of keying it on the lockfile alone. Kept in sync with
# those modules by tests/team/test_release_compose_artifacts.py.
# Full OpenCV needs the explicitly installed libGL/GLib/XCB runtime libraries;
# import it directly before the model warm-up so a slim-image ABI regression is
# reported as an OpenCV provisioning failure rather than a later OCR failure.
# --no-sync: src/ is not present yet, so `uv run`'s implicit project sync
# (which would rebuild the frisket root package) must be skipped.
RUN uv run --no-sync python -c "import cv2" \
    && uv run --no-sync python -c "from fastembed import TextEmbedding; \
    TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" \
    && uv run --no-sync python -c "from rapidocr import RapidOCR; from rapidocr.utils.typings import OCRVersion; RapidOCR(params={'Rec.ocr_version': OCRVersion.PPOCRV5})" \
    && uv run --no-sync python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; \
    TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')"
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev $FRISKET_UV_EXTRAS
COPY --from=web /build/dist /app/static

# Published/product images carry their exact source identity without shipping
# .git. Declare the changing arg after the expensive dependency/model layers so
# a new source SHA does not invalidate those caches.
ARG FRISKET_BUILD_SHA
RUN case "$FRISKET_BUILD_SHA" in \
      ''|*[!0123456789abcdef]*) \
        echo 'FRISKET_BUILD_SHA must be the full lowercase 40-hex source SHA' >&2; \
        exit 1; \
        ;; \
    esac \
    && if [ "${#FRISKET_BUILD_SHA}" -ne 40 ]; then \
      echo 'FRISKET_BUILD_SHA must be the full lowercase 40-hex source SHA' >&2; \
      exit 1; \
    fi \
    && printf '%s\n' "$FRISKET_BUILD_SHA" > VERSION

# Non-root runtime identity — part of the downstream-overlay contract that
# .github/workflows/docker.yml's "Assert the downstream overlay contract"
# step enforces: fixed uid/gid 10001:10001, high enough to never collide
# with a real host account on bind-mounted data roots. /data is
# created and chowned here so a fresh named volume inherits frisket ownership
# on first mount; a pre-existing root-owned /data from an older (root-running)
# image fails the startup writability check with the chown fix in the message
# (see the startup error's one-time ownership repair). The baked
# model caches (/tmp/fastembed_cache, site-packages rapidocr models) stay
# root-owned: they are read-only at runtime and load as uid 10001 — proven by
# the docker.yml overlay-contract CI step.
RUN groupadd --gid 10001 frisket \
    && useradd --uid 10001 --gid frisket --create-home --home-dir /home/frisket \
      --shell /usr/sbin/nologin frisket \
    && mkdir --parents /data \
    && chown frisket:frisket /data

ENV FRISKET_STATIC_DIR=/app/static/team \
    FRISKET_DATA_DIR=/data \
    HF_HUB_CACHE=/data/.cache/huggingface/hub \
    FRISKET_EXIFTOOL_PATH=/usr/local/bin/exiftool \
    FRISKET_FFPROBE_PATH=/usr/bin/ffprobe \
    PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000
USER frisket
# Shell-form CMD deliberately expands the platform-provided PORT at *container*
# runtime.  The server defaults to 8000 when a host does not supply one.
# Readiness remains available while the instance is unclaimed and additionally
# proves a matching worker has heartbeated, so a web-only zombie is not green.
HEALTHCHECK --interval=30s --timeout=5s \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/ready" || exit 1
CMD ["frisket", "server"]
