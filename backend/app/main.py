"""YouTube Downloader FastAPI application."""

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import __version__
from app.limiter import limiter

logger = logging.getLogger(__name__)

RATE_LIMIT_WINDOW_SECONDS = 60


def _debug_enabled() -> bool:
    """Whether the app is running in development mode."""
    return os.environ.get("DEBUG", "false").lower() == "true"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI instance with CORS middleware and exception handlers.
    """
    # The interactive docs are a development aid, not part of the
    # public surface: the page is unauthenticated and pulls Swagger UI
    # from a third-party CDN, which a privacy-first self-hosted service
    # should not make its visitors contact.
    debug = _debug_enabled()
    app = FastAPI(
        title="YouTube Downloader API",
        version=__version__,
        docs_url="/api/docs" if debug else None,
        openapi_url="/api/openapi.json" if debug else None,
    )

    _configure_cors(app)
    _configure_rate_limiter(app)
    _configure_exception_handlers(app)
    _include_routers(app)
    _add_health_check(app)

    return app


def _configure_rate_limiter(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "rate_limited",
                "message": (
                    f"Rate limit exceeded: {exc.detail}. "
                    "Please slow down and try again."
                ),
            }
        },
        # Every configured limit uses a one-minute window, so the
        # bucket has definitely refilled by then.
        headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
    )


def _configure_cors(app: FastAPI) -> None:
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if raw:
        allowed_origins = [
            origin.strip() for origin in raw.split(",") if origin.strip()
        ]
    else:
        if not _debug_enabled():
            raise RuntimeError(
                "ALLOWED_ORIGINS must be set explicitly in production. "
                "Use a comma-separated list of origins (e.g. https://example.com), "
                "or set DEBUG=true to allow all origins for local development."
            )
        logger.warning(
            "ALLOWED_ORIGINS not set; allowing all origins because DEBUG=true."
        )
        allowed_origins = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )


def _configure_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled exception")
        detail = str(exc) if _debug_enabled() else "Internal server error"
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": detail}},
        )


def _add_health_check(app: FastAPI) -> None:
    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}


def _include_routers(app: FastAPI) -> None:
    from app.routers.download import router as download_router
    from app.routers.info import router as info_router

    app.include_router(info_router)
    app.include_router(download_router)
