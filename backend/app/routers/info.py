"""API endpoints for video and playlist information."""

from urllib.parse import urlparse

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.limiter import limiter
from app.routers.shared import error_response
from app.schemas.video import ErrorEnvelope, PlaylistInfo, VideoInfo
from app.services.youtube import (
    InvalidURLError,
    PlaylistTooLargeError,
    VideoNotFoundError,
    YouTubeError,
    extract_playlist_info,
    extract_video_info,
)

router = APIRouter(prefix="/api", tags=["info"])


@router.get(
    "/info",
    response_model=VideoInfo | PlaylistInfo,
    responses={
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
@limiter.limit("30/minute")
def get_info(
    request: Request,
    url: str = Query(..., description="YouTube video or playlist URL"),
) -> VideoInfo | PlaylistInfo | JSONResponse:
    """Retrieve metadata for a YouTube video or playlist.

    Declared sync on purpose: yt-dlp extraction is blocking network
    I/O, and FastAPI runs sync endpoints in a worker thread. As an
    ``async def`` it ran on the single event loop instead, so one
    metadata request froze every other request -- including in-flight
    downloads -- for its whole duration.

    Args:
        url: YouTube video or playlist URL.

    Returns:
        VideoInfo for single videos, PlaylistInfo for playlists.
    """
    try:
        if _is_playlist_url(url):
            return extract_playlist_info(url)
        return extract_video_info(url)
    except InvalidURLError as e:
        return error_response(400, "invalid_url", str(e))
    except PlaylistTooLargeError as e:
        # The message names the limit, so the caller can act on it.
        return error_response(400, "playlist_too_large", str(e))
    except VideoNotFoundError as e:
        return error_response(
            404,
            "not_found",
            "The requested video or playlist is not available.",
            detail=str(e),
        )
    except YouTubeError as e:
        return error_response(
            500,
            "extraction_error",
            "Could not process this URL. Please try a different one.",
            detail=str(e),
        )


def _is_playlist_url(url: str) -> bool:
    """Detect playlist URLs by path, not query parameters.

    URLs like watch?v=abc&list=PLxxx are treated as single videos
    since the user's intent is to download that specific video.
    Only /playlist paths are treated as playlist requests.
    """
    parsed = urlparse(url)
    return parsed.path.rstrip("/").endswith("/playlist")
