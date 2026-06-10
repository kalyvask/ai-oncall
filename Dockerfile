# Multi-stage build. Stage 1 builds the Next.js bundle; stage 2 installs
# Python deps and copies the bundle in. Final image runs FastAPI which
# serves the SPA from /web.

# ---------- Stage 1: web build ------------------------------------------
FROM node:20-alpine AS web-builder
WORKDIR /app/web
COPY web/package.json web/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY web/ ./
RUN npm run build

# ---------- Stage 2: runtime --------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root user. Mounted volumes (data/) must be writable by uid 10001.
RUN groupadd --system --gid 10001 oncall \
 && useradd --system --uid 10001 --gid oncall --home-dir /app --shell /usr/sbin/nologin oncall

WORKDIR /app

# Install third-party deps first so the layer cache survives source edits.
# `pip install .` can't run here — hatchling refuses to build a wheel before
# the ai_oncall/ source is copied in — so extract the dependency list from
# pyproject.toml and install just those.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
 && python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt anthropic httpx

COPY ai_oncall ./ai_oncall
COPY schemas ./schemas
COPY runbooks ./runbooks
COPY topology.yaml ./topology.yaml

# Source is present now; install the package itself without re-resolving deps.
RUN pip install --no-cache-dir --no-deps .

# Copy the built Next.js bundle into /app/web so a future static handler
# (or a reverse-proxy sidecar) can serve it. The FastAPI process itself
# only serves the API in v1.
COPY --from=web-builder /app/web/.next /app/web/.next
COPY --from=web-builder /app/web/public /app/web/public
COPY --from=web-builder /app/web/package.json /app/web/package.json

RUN mkdir -p /app/data && chown -R oncall:oncall /app
USER oncall

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request, sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status == 200 else sys.exit(1)" || exit 1

CMD ["uvicorn", "ai_oncall.server:app", "--host", "0.0.0.0", "--port", "8000"]
