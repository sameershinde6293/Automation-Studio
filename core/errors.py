"""Autopilot error categories and user-facing error handling.

All custom exceptions inherit from AutopilotError. User-facing messages
must never expose stack traces or internal paths without sanitization.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AutopilotError(Exception):
    """Base exception for all Autopilot errors."""

    error_code: str = "UNKNOWN_ERROR"
    user_message: str = "An unexpected error occurred."
    is_recoverable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        user_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize error.

        Args:
            message: Technical message for logs.
            user_message: Optional override for user-facing text.
            details: Optional structured context for logs.
        """
        self.technical_message = message or self.user_message
        if user_message is not None:
            self.user_message = user_message
        self.details = details or {}
        super().__init__(self.technical_message)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize error for API/UI responses.

        Returns:
            Dictionary with code, messages, and recoverability.
        """
        return {
            "error_code": self.error_code,
            "user_message": self.user_message,
            "technical_message": self.technical_message,
            "is_recoverable": self.is_recoverable,
            "details": self.details,
        }


class ScriptImportError(AutopilotError):
    """Raised when script or project import fails."""

    error_code = "IMPORT_ERROR"
    user_message = "Could not import the selected files. Please check the file format."
    is_recoverable = True


class ScriptParseError(AutopilotError):
    """Raised when a script cannot be parsed."""

    error_code = "SCRIPT_PARSE_ERROR"
    user_message = (
        "The script could not be read. Please check the format and try again."
    )
    is_recoverable = True


class ImageProcessingError(AutopilotError):
    """Raised when image processing fails."""

    error_code = "IMAGE_PROCESSING_ERROR"
    user_message = (
        "An image could not be processed. Check that the file is a valid image."
    )
    is_recoverable = True


class TTSError(AutopilotError):
    """Raised when text-to-speech generation fails."""

    error_code = "TTS_ERROR"
    user_message = (
        "Voice generation failed. Try another engine or reinstall voice models."
    )
    is_recoverable = True


class AudioProcessingError(AutopilotError):
    """Raised when audio mixing or effects fail."""

    error_code = "AUDIO_PROCESSING_ERROR"
    user_message = "Audio processing failed. Check music and narration files."
    is_recoverable = True


class FFmpegError(AutopilotError):
    """Raised when an FFmpeg subprocess fails."""

    error_code = "FFMPEG_ERROR"
    user_message = "Video processing failed. Ensure FFmpeg is installed (run setup)."
    is_recoverable = True


class DatabaseError(AutopilotError):
    """Raised when a database operation fails."""

    error_code = "DATABASE_ERROR"
    user_message = "A data storage error occurred. Try restarting Autopilot."
    is_recoverable = True


class LicenseError(AutopilotError):
    """Raised when license validation fails."""

    error_code = "LICENSE_ERROR"
    user_message = "License validation failed. Enter a valid license key."
    is_recoverable = True


class RenderError(AutopilotError):
    """Raised when the render pipeline fails unrecoverably."""

    error_code = "RENDER_ERROR"
    user_message = "Render failed. Check the render log for details."
    is_recoverable = True


class DiskSpaceError(AutopilotError):
    """Raised when free disk space is insufficient."""

    error_code = "DISK_SPACE_ERROR"
    user_message = "Not enough free disk space to continue. Free space and try again."
    is_recoverable = True


class ExportError(AutopilotError):
    """Raised when final export fails."""

    error_code = "EXPORT_ERROR"
    user_message = (
        "Could not export the final video. Check the output folder permissions."
    )
    is_recoverable = True


class SubtitleError(AutopilotError):
    """Raised when subtitle generation or burn-in fails."""

    error_code = "SUBTITLE_ERROR"
    user_message = "Subtitle processing failed. You can continue without subtitles."
    is_recoverable = True


class CacheError(AutopilotError):
    """Raised when cache read/write fails."""

    error_code = "CACHE_ERROR"
    user_message = "Cache operation failed. Autopilot will continue without cache."
    is_recoverable = True


class ConfigError(AutopilotError):
    """Raised when configuration is missing or invalid."""

    error_code = "CONFIG_ERROR"
    user_message = "A settings file is missing or corrupted. Defaults will be used."
    is_recoverable = True


class HardwareError(AutopilotError):
    """Raised when hardware detection or acceleration setup fails."""

    error_code = "HARDWARE_ERROR"
    user_message = (
        "Hardware acceleration is unavailable. Software encoding will be used."
    )
    is_recoverable = True


def format_user_error(error: BaseException) -> str:
    """Return a user-friendly message for any exception.

    Args:
        error: Exception instance.

    Returns:
        Safe user-facing string.
    """
    if isinstance(error, AutopilotError):
        return error.user_message
    return "An unexpected error occurred. Please try again."


def make_error_response(
    error: BaseException,
    module: str = "core",
) -> Dict[str, Any]:
    """Build a standard failure response from an exception.

    Args:
        error: Exception instance.
        module: Module name for the response.

    Returns:
        Standard module response dictionary with success=False.
    """
    from core.time_helper import utc_now_str

    if isinstance(error, AutopilotError):
        payload = error.to_dict()
        message = error.user_message
    else:
        payload = {
            "error_code": "UNKNOWN_ERROR",
            "user_message": format_user_error(error),
            "technical_message": str(error),
            "is_recoverable": False,
            "details": {},
        }
        message = payload["user_message"]

    return {
        "success": False,
        "data": payload,
        "error": message,
        "warnings": [],
        "module": module,
        "timestamp": utc_now_str(),
    }
