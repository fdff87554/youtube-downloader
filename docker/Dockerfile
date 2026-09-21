# Stage 0: Pull a pinned Deno binary. yt-dlp uses an external JavaScript
# runtime to solve YouTube player challenges; Deno is the upstream
# recommended choice and ships as a single static binary.
FROM denoland/deno:bin-2.9.7 AS deno-bin

# Stage 1: Build frontend
FROM node:26.9.0-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2: Install backend dependencies from the pinned lockfile.
# The application package itself is mounted via PYTHONPATH in stage 3, so we only
# need the third-party dependencies here.
FROM python:3.12.13-slim-bookworm AS backend-deps
WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip
COPY backend/requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock

# Stage 3: Runtime
FROM python:3.12.13-slim-bookworm

# Install nginx (reverse proxy + static files), ffmpeg (audio/video muxing
# for yt-dlp), and curl (used by HEALTHCHECK).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the Deno binary from stage 0 so yt-dlp can reach the full set of
# YouTube formats. Without a JavaScript runtime, yt-dlp falls back to a
# limited set of formats and may fail for some videos.
COPY --from=deno-bin /deno /usr/local/bin/deno

# Copy Python dependencies
COPY --from=backend-deps /install /usr/local

# Copy backend source
WORKDIR /app
COPY backend/app/ ./app/

# Copy built frontend
COPY --from=frontend-build /build/dist /var/www/html

# Copy nginx config
COPY docker/nginx.conf /etc/nginx/nginx.conf

# Copy entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run as a non-root user. Create the account, give it ownership of the
# directories nginx writes to (cache and lib state), and switch to it
# for the rest of the image. Logs go to stdout/stderr (configured in
# nginx.conf) so /var/log/nginx does not need to be writable.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home --home-dir /home/app app \
    && mkdir -p /var/lib/nginx /var/lib/nginx/body /var/lib/nginx/proxy \
    /var/lib/nginx/fastcgi /var/lib/nginx/uwsgi /var/lib/nginx/scgi \
    && chown -R app:app /var/lib/nginx
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
