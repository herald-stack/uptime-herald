# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
#
# Dockerfile - Builds the Uptime Herald application image.
#
# Pinned to a specific patch release plus the registry's multi-arch manifest
# digest, so a build today and a build a year from now use byte-identical
# base layers. Digest verified against Docker Hub's registry API for
# python:3.14.7-slim-trixie (index digest, covers all published
# platforms — re-verify and update if you bump the tag).
FROM python:3.14.7-slim-trixie@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS base

# --- The default config and theme files -----------------------------------------------
# The image carries a default config.yaml and theme.yaml. A build uses the
# config.default.yaml and theme.default.yaml in the build folder when it has them.
# For any it lacks, it downloads that version's copy from get.uptimeherald.org, so a
# plain `docker build` of a release works as it is. The wildcards let the COPY succeed
# when neither file is in the build folder, and curl exists only in this stage.
FROM base AS defaults
WORKDIR /defaults
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY src/config.py config.default.yaml* theme.default.yaml* ./
RUN version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' config.py) \
    && for name in config.default.yaml theme.default.yaml; do \
         [ -e "$name" ] || curl -fsSL -o "$name" \
           "https://get.uptimeherald.org/releases/$version/config/$name" || exit 1; \
       done

# --- The application image ------------------------------------------------------------
FROM base

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Dedicated non-root user/group the app runs as. iputils-ping on Debian
# uses a file capability (cap_net_raw) rather than setuid, so ICMP checks
# keep working for a non-root process as long as the container retains the
# default CAP_NET_RAW capability (Docker grants this by default; don't
# add cap_drop: [ALL] / cap_drop: [NET_RAW] for this service in compose).
RUN groupadd --system --gid 1000 uptimeherald \
    && useradd --system --uid 1000 --gid uptimeherald --home-dir /app \
       --no-create-home --shell /usr/sbin/nologin uptimeherald

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY locales/ locales/
# A single /app/config directory (bind-mounted wholesale by compose, one
# mount instead of six fragile per-file ones -- Docker auto-creates a missing
# individual file's bind-mount source as an empty directory, which mismatches
# the real file baked into the image and crashes the container). Only
# config.yaml/theme.yaml get real defaults here: load_config() requires
# config.yaml to exist, but treats a missing
# alerts.yaml/monitors.yaml/tags.yaml/maintenance.yaml as "use defaults", so
# those four are intentionally absent.
COPY --from=defaults /defaults/config.default.yaml config/config.yaml
COPY --from=defaults /defaults/theme.default.yaml config/theme.yaml

# Give the non-root user ownership of /app.
RUN chown -R uptimeherald:uptimeherald /app

USER uptimeherald

EXPOSE 8080

# Hits the app's own /health endpoint (src/main.py). No curl/wget in this
# image and neither is worth adding just for this — python3 is already
# here, so use it directly instead of pulling in a new package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request as u; import sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
