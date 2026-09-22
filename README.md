# YouTube Downloader

A privacy-first, self-hosted YouTube downloader. Downloads stream directly
from YouTube to the user's browser through the server, without the server
ever writing the video to disk.

## Features

- **No disk I/O for media.** Videos are piped from `yt-dlp` straight to the
  HTTP response. The server never stores user downloads.
- **Stateless backend.** No database, no sessions, no per-user state.
- **Single-container deployment.** `nginx` serves the static frontend and
  reverse-proxies the API to `uvicorn` in one image.
- **MP4 video and MP3 audio.** Quality selection up to the best available.
- **Playlist support.** Browse playlist entries and download videos one by
  one.
- **Reproducible builds.** Backend dependencies are pinned in
  `requirements.lock`; frontend dependencies are pinned in `package-lock.json`.

## Architecture

```text
Browser
   |
   v
+-----------------------------+
| nginx (port 8080)           |
|  - serves /var/www/html     |   (built frontend)
|  - reverse-proxy /api/* --> |
+-----------------------------+
                              |
                              v
                +------------------------+
                | uvicorn (127.0.0.1:8000)|
                | FastAPI app             |
                |   /api/info             |
                |   /api/download         |
                |   /api/health           |
                +------------------------+
                              |
                              v
                +-------------------------+
                | yt-dlp (+ ffmpeg, deno) |
                +-------------------------+
                              |
                              v
                          YouTube
```

The download endpoint returns a `StreamingResponse` whose body is the live
output of `yt-dlp`'s subprocess (and `ffmpeg` for MP3). Nginx is configured
with `proxy_buffering off` so the bytes flow straight through to the client.

## Quick start (Docker)

```bash
# Local development: DEBUG=true makes CORS fall back to wildcard so the
# container can start without an ALLOWED_ORIGINS value. See "Configuration"
# below for production usage.
DEBUG=true docker compose up --build -d

# Visit http://localhost:8080
# Health check
curl http://localhost:8080/api/health
```

For a production-like local run (still recommended for any non-loopback
deployment), set `ALLOWED_ORIGINS` to the origin you will browse from
instead of `DEBUG=true`:

```bash
ALLOWED_ORIGINS=http://localhost:8080 docker compose up --build -d
```

To use a different host port:

```bash
DEBUG=true PORT=9000 docker compose up -d
```

## Configuration

The container reads the following environment variables:

| Variable          | Default    | Description                                |
| ----------------- | ---------- | ------------------------------------------ |
| `ALLOWED_ORIGINS` | _required_ | Comma-separated CORS origins (see below)   |
| `DEBUG`           | `false`    | When `true`, errors include exception text |
| `PORT`            | `8080`     | Host port forwarded to the container       |
| `TRUSTED_PROXIES` | _empty_    | Comma-separated CIDRs of upstream proxies  |

`ALLOWED_ORIGINS` must be set to the origin(s) that may call the API,
e.g. `https://example.com,https://x.example.com`. The application
refuses to start with an empty value unless `DEBUG=true`, in which case
all origins are permitted (for local development only).

### `TRUSTED_PROXIES`

Set this whenever another reverse proxy (Caddy, nginx, Cloudflare, a
load balancer) sits in front of the container:

```bash
TRUSTED_PROXIES=10.0.0.0/8,172.16.0.0/12 docker compose up -d
```

Rate limiting is per client IP, at both the nginx and the application
layer. Without `TRUSTED_PROXIES` the container only sees the upstream
proxy's address, so every visitor shares one quota and a single busy
client can return `429` to everyone. Leave it empty when the container
is directly exposed.

## Development

### Prerequisites

Install [mise](https://mise.jdx.dev/) and let it set up the toolchain:

```bash
mise install
```

This provides Python 3.14, Node 22, `pre-commit`, and the
formatters/linters used by the project (`ruff`, `yamlfmt`, `shfmt`,
`shellcheck`).

### Backend

```bash
pip install -r backend/requirements-dev.lock
pip install -e backend --no-deps
cd backend && pytest -v
```

To regenerate the lockfiles after editing `backend/pyproject.toml`:

```bash
pip install pip-tools
pip-compile backend/pyproject.toml -o backend/requirements.lock
pip-compile backend/pyproject.toml --extra dev -o backend/requirements-dev.lock
```

### Frontend

```bash
cd frontend
npm ci
npm run dev    # dev server on http://localhost:5173, proxies /api to :8000
npm run build  # production build into frontend/dist
```

Run the backend
(`DEBUG=true uvicorn --factory app.main:create_app --port 8000`) at the
same time so the dev server's `/api` proxy has somewhere to forward to.

### Pre-commit hooks

```bash
pre-commit install
pre-commit run --all-files
```

Hooks cover Python (`ruff`), YAML (`yamlfmt`), shell scripts (`shfmt`,
`shellcheck`), and Markdown/JSON (`prettier`, `markdownlint-cli2`).

## API reference

Interactive Swagger UI is available at `/api/docs` when the server is
running.

### `GET /api/info?url=<youtube-url>`

Returns metadata. For a video URL, returns `VideoInfo`; for a `/playlist`
URL, returns `PlaylistInfo`.

### `GET /api/download?url=<url>&fmt=mp4|mp3&quality=best|1080|720|480&title=<filename>`

Streams the media as `video/mp4` or `audio/mpeg` with `Content-Disposition:
attachment`.

### `GET /api/health`

Returns `{"status": "ok"}` when the application is responsive.

### Error format

Errors use a unified envelope:

```json
{
  "error": {
    "code": "invalid_url",
    "message": "URL must be a valid YouTube URL."
  }
}
```

## What gets logged

The container writes to stdout/stderr only; no log file is written
inside the image.

- **Access log**: client IP, timestamp, method, path, status, response
  size and duration. Query strings are excluded, so the video URL and
  title a visitor requested do not appear in this log.
- **nginx error log**: nginx writes its own diagnostics (rate-limit
  rejections, upstream failures) with the **full request line**, query
  string included. So a rate-limited request does record which video
  was asked for. Only the access log format is under this project's
  control; nginx does not offer a format for these entries.
- **Application log**: warnings and errors, including `yt-dlp` stderr
  when a download fails. A failing URL can appear here.

How long any of this is kept is up to your container log driver, which
this project does not configure.

## Deployment notes

- The container listens on port `8080` inside the network. Expose it as
  needed and put a TLS-terminating reverse proxy (Caddy, Cloudflare, etc.)
  in front for production.
- `proxy_read_timeout`/`proxy_send_timeout` are set to `600s` to support
  long downloads. Adjust if your reverse proxy has stricter limits.
- A client may run 3 concurrent `/api/*` requests; further ones get
  `429` until one finishes. Raise `limit_conn api_conn` in
  `docker/nginx.conf` if your users legitimately download more at once.
- Set `ALLOWED_ORIGINS` and (where supported) configure rate limiting at
  the reverse-proxy layer for any internet-facing deployment.
- Set `TRUSTED_PROXIES` to your proxy's network whenever one is in front
  of the container, or the built-in per-IP rate limits degrade into a
  single shared quota.
- The container runs with a read-only root filesystem and a tmpfs on
  `/tmp`. If you write your own compose file or run `docker run`
  directly, carry both over: `--read-only --tmpfs /tmp`. Without the
  tmpfs the container cannot start; without `--read-only` it still
  works, but nothing then stops a write to the image layer.
- What that guarantees precisely: **nothing is written to the
  container's writable layer**. It is not a guarantee that no byte
  reaches a disk — Docker's own
  [tmpfs documentation](https://docs.docker.com/engine/storage/tmpfs/)
  notes that "the temporary data may be written to a swap file, and
  thereby persisted to the filesystem". If that matters for your
  threat model, run the host without swap, or with encrypted swap.

## Troubleshooting

- **`ALLOWED_ORIGINS` startup failure.** The container exits with
  `RuntimeError: ALLOWED_ORIGINS must be set explicitly in production`
  when `ALLOWED_ORIGINS` is unset and `DEBUG` is not `true`. Set
  `ALLOWED_ORIGINS` in the deployment environment to the origin(s) the
  browser uses to reach the service (e.g.
  `https://your-domain.example`); for local development, set
  `DEBUG=true` instead. See [Configuration](#configuration) for details.
- **Download stops mid-stream.** The browser will show a partial file. Try
  again, choose a different quality, or check the container logs (yt-dlp
  errors are surfaced via `logger.error`).
- **`yt-dlp` fails on a specific video.** Pull the latest image; YouTube
  player changes occasionally require an updated `yt-dlp` build.
- **Empty playlist response.** Make sure the URL contains a `/playlist`
  path. URLs like `watch?v=abc&list=PLxxx` are treated as a single video.

## Legal

Please read [LEGAL.md](LEGAL.md) before using or hosting this software.
**You are responsible** for ensuring your use complies with applicable
laws and YouTube's Terms of Service.

## License

Released under the [MIT License](LICENSE).
