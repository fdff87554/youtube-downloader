"""Shared utilities for API routers."""

import logging
import os

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Both /api/info and /api/download answer UnsupportedURLError with this,
# so the wording lives in one place.
FORMAT_UNAVAILABLE_MESSAGE = (
    "This video is not available in the quality you asked for. Try a different quality."
)
UNSUPPORTED_URL_MESSAGE = (
    "This YouTube URL is not a video or playlist that can be downloaded. "
    "Check the link and try again."
)

# The service never signs in, so an age gate is terminal: no retry and no
# other quality reaches the video.
AGE_RESTRICTED_MESSAGE = (
    "YouTube requires a signed-in, age-verified account for this video, "
    "and this service does not sign in."
)
# Says nothing about the video: YouTube has flagged this deployment's IP.
# Phrased for the person who pasted the link, while the code and the logged
# detail tell the operator which lever to pull.
BOT_CHECK_MESSAGE = (
    "YouTube is currently refusing automated requests from this server. "
    "Try again later, or ask the operator to check the service."
)


def error_response(
    status_code: int,
    code: str,
    message: str,
    detail: str | None = None,
) -> JSONResponse:
    """Build a unified error response.

    The client always sees ``message`` (a user-safe description) in the
    standard envelope. ``detail`` carries the raw upstream error text
    (e.g. yt-dlp output) and is logged for operators; it is only
    surfaced to the client when ``DEBUG=true`` so production responses
    do not leak internal information.

    Args:
        status_code: HTTP status code.
        code: Machine-readable error code.
        message: Human-readable, user-safe error description.
        detail: Optional raw error text for logs / debug responses.

    Returns:
        JSONResponse with the standard error envelope.
    """
    if detail:
        logger.warning("%s (code=%s): %s", message, code, detail)
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    body_message = detail if debug and detail else message
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": body_message}},
    )
