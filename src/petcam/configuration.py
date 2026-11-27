"""Validate the USB camera mode used by the VLC stream."""

from __future__ import annotations

import re
from typing import Any


class ConfigurationError(ValueError):
    """Raised when the requested camera mode is invalid."""


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError("Camera configuration must be an object.")
    if str(value.get("source", "")).upper() != "USB":
        raise ConfigurationError("Only USB cameras with native H.264 output are supported.")
    resolution = str(value.get("resolution", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", resolution):
        raise ConfigurationError("Resolution must use WIDTHxHEIGHT format.")
    try:
        fps = float(value.get("fps"))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Frame rate must be a positive number.") from error
    if not 0 < fps <= 1000:
        raise ConfigurationError("Frame rate must be greater than 0 and no more than 1000 FPS.")
    device = str(value.get("device", ""))
    if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)", device):
        raise ConfigurationError("USB camera device path is invalid.")
    audio = value.get("audio", {})
    if not isinstance(audio, dict) or audio.get("enabled", True) is False:
        raise ConfigurationError("The combined /stream endpoint requires microphone audio.")
    alsa_device = str(audio.get("device", "auto"))
    if not alsa_device or any(char in alsa_device for char in "\r\n"):
        raise ConfigurationError("ALSA capture device is invalid.")
    return {
        "source": "USB", "device": device,
        "physical_path": str(value.get("physical_path", "")),
        "resolution": resolution, "fps": int(fps) if fps.is_integer() else fps,
        "audio": {
            "enabled": True, "sample_rate": 44100, "channels": 2,
            "input_format": "S16_LE", "output_codec": "MP3",
            "bitrate": 128000, "device": alsa_device,
        },
    }
