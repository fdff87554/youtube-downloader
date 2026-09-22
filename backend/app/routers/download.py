"""API endpoint for streaming video/audio downloads."""

from collections.abc import Generator
from enum import StrEnum
from itertools import chain
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.limiter import limiter
from app.routers.shared import error_response
from app.schemas.video import ErrorEnvelope
from app.services.youtube import (
    InvalidURLError,
    VideoNotFoundError,
    YouTubeError,
    build_download_filename,
    stream_download,
    validate_youtube_url,
)

router = APIRouter(prefix="/api", tags=["download"])


class FormatType(StrEnum):
    """Supported download format types."""

    MP4 = "mp4"
    MP3 = "mp3"


class Quality(StrEnum):
    """Supported video quality levels."""

    BEST = "best"
    Q1080 = "1080"
    Q720 = "720"
    Q480 = "480"


MEDIA_TYPES = {
    FormatType.MP4: "video/mp4",
    FormatType.MP3: "audio/mpeg",
}


@router.get(
    "/download",
    response_model=None,
    responses={
        200: {"content": {"video/mp4": {}, "audio/mpeg": {}}},
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
@limiter.limit("5/minute")
def download_video(
    request: Request,
    url: str = Query(..., description="YouTube video URL"),
    fmt: FormatType = FormatType.MP4,
    quality: Quality = Quality.BEST,
    title: str | None = Query(None, description="Video title for filename"),
) -> StreamingResponse | JSONResponse:
    """Stream a YouTube video or audio download.

    Pipes yt-dlp output directly to the HTTP response with zero disk I/O.

    Declared sync on purpose: _start_stream blocks until yt-dlp has
    produced its first bytes, which can take seconds. FastAPI runs sync
    endpoints in a worker thread; as an ``async def`` that wait ran on
    the single event loop and stalled every other request, including
    in-flight downloads and the health check.

    Args:
        url: YouTube video URL.
        fmt: Output format (mp4 or mp3).
        quality: Video quality (best, 1080, 720, 480).
        title: Optional video title for the download filename.

    Returns:
        StreamingResponse with the media content.
    """
    try:
        validate_youtube_url(url)
        filename = build_download_filename(title, fmt.value)
        media_type = MEDIA_TYPES[fmt]
        encoded_filename = quote(filename)

        stream = stream_download(url, fmt.value, quality.value)
        first_chunk = _start_stream(stream)

        return StreamingResponse(
            content=chain((first_chunk,), stream),
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f"attachment; "
                    f'filename="{encoded_filename}"; '
                    f"filename*=UTF-8''{encoded_filename}"
                ),
            },
        )
    except InvalidURLError as e:
        return error_response(400, "invalid_url", str(e))
    except VideoNotFoundError as e:
        return error_response(
            404,
            "not_found",
            "The requested video is not available.",
            detail=str(e),
        )
    except YouTubeError as e:
        return error_response(
            500,
            "download_error",
            "Could not start the download. Please try again.",
            detail=str(e),
        )


def _start_stream(stream: Generator[bytes]) -> bytes:
    """Pull the first chunk so failures happen before the headers do.

    ``stream_download`` is a generator function: calling it runs none of
    its body, so a yt-dlp failure would otherwise surface only after
    StreamingResponse has already written a 200 status line, leaving no
    way to send the error envelope. Advancing it once here moves that
    failure into the caller's try/except.

    Raises:
        VideoNotFoundError: If the download produced no data at all.
        YouTubeError: For any other download failure.
    """
    try:
        return next(stream)
    except StopIteration:
        raise VideoNotFoundError("The download produced no data.") from None
