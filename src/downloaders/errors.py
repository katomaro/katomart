"""Shared exception types for the download layer.

These let the retry layer in ``src/app/workers.py`` distinguish failures that
are worth retrying (transient: connection drops, 5xx, 429, timeouts) from those
that never will be (permanent: 404, unsupported URL, DRM, dead invite links).

The downloader contract returns ``bool`` (True/False) for success/failure. A
bare ``False`` is treated as a *transient* failure by the retry layer, so it is
retried — matching the user expectation that a failed video download is tried
again. Downloaders raise :class:`PermanentDownloadError` instead of returning
``False`` when they can prove the failure is permanent, so the retry layer skips
the (pointless) retries and their backoff delays.
"""

from __future__ import annotations


class DownloadError(Exception):
    """Base class for download-layer errors."""


class PermanentDownloadError(DownloadError):
    """A download failed for a reason that retrying cannot fix.

    Examples: HTTP 404 (resource gone), 403 after auth fallback, an
    "Unsupported URL" from yt-dlp's generic extractor (the link is not a video,
    e.g. a community invite link), DRM-protected media without a CDM, or a
    malformed URL. The retry layer must NOT retry these.
    """


class TransientDownloadError(DownloadError):
    """A download failed for a reason that may succeed on retry.

    Examples: connection reset, remote disconnect, read timeout, HTTP 429/5xx.
    The retry layer SHOULD retry these. Downloaders may also signal a transient
    failure simply by returning ``False`` (the historical contract).
    """
