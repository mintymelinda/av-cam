#!/usr/bin/env python3
"""Configure a USB camera and publish one combined H.264 + MP3 HTTP stream."""

from __future__ import annotations

import argparse
import curses
import json
import logging
import math
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .configuration import ConfigurationError, validate_config
from .domain.camera.capabilities import mode_fps_values
from .infrastructure.camera.discovery import (
    discover_usb_cameras,
)
from .infrastructure.system.metrics import ResourceMetrics
from .infrastructure.system.hostname import advertised_hostname

RUN_DIR = Path(os.environ.get("PETCAM_RUN", "/run/petcam"))
STREAM_STATUS = RUN_DIR / "stream-status.json"
STREAM_LOG = Path(os.environ.get("PETCAM_STREAM_LOG", "/var/log/petcam/stream.log"))
CONFIG_FILE = Path(os.environ.get("PETCAM_CONFIG", "/etc/petcam/config.json"))
HTTP_PORT = 8080
HTTP_PATH = "/stream"
HOTPLUG = object()


def parse_short_config(value: str) -> tuple[str, str, float]:
    """Parse the documented SOURCE;WIDTHxHEIGHT;FPS shortcut syntax."""
    parts = value.split(";")
    if len(parts) != 3:
        raise ConfigurationError('Use -c "USB;1920x1080;30".')
    source, resolution, raw_fps = (part.strip() for part in parts)
    source = source.upper()
    if source != "USB":
        raise ConfigurationError("Only USB cameras with native H.264 output are supported.")
    if not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", resolution):
        raise ConfigurationError("Resolution in -c must use WIDTHxHEIGHT format.")
    try:
        fps = float(raw_fps)
    except ValueError as error:
        raise ConfigurationError("Frame rate in -c must be a positive number.") from error
    if not math.isfinite(fps) or not 0 < fps <= 1000:
        raise ConfigurationError("Frame rate must be greater than 0 and no more than 1000 FPS.")
    return source, resolution, fps


def discover_cameras(source: str) -> list[dict]:
    if source == "USB":
        return discover_usb_cameras()
    raise ValueError("Only USB cameras are supported.")


def camera_modes(source: str, camera: dict) -> list[dict]:
    """Return only H.264 modes available to the selected camera and encoder."""
    options: dict[tuple[str, float], dict] = {}
    if source == "USB":
        for mode in camera.get("modes", []):
            if str(mode.get("format", "")).upper() != "H264":
                continue
            for fps in mode_fps_values(mode):
                key = (f'{mode["width"]}x{mode["height"]}', fps)
                options[key] = {"resolution": key[0], "fps": fps, "format": "H264"}
    return sorted(options.values(), key=lambda item: (-int(item["resolution"].split("x")[0]), -item["fps"]))


def _camera_label(source: str, camera: dict) -> str:
    if source == "USB":
        return f'{camera.get("label") or "USB camera"} ({camera.get("device", "unknown device")})'
    return f'{camera.get("sensor", "USB camera")} (camera {camera.get("index", "?")})'


def _camera_identity(source: str, camera: dict) -> tuple:
    if source == "USB":
        return (camera.get("device"), camera.get("physical_path"))
    return (camera.get("device"), camera.get("physical_path"))


def inventory_signature(source: str, cameras: list[dict]) -> str:
    """Include camera identities and advertised modes to detect live list changes."""
    normalized = []
    for camera in cameras:
        normalized.append({
            "identity": _camera_identity(source, camera),
            "modes": camera.get("modes", []),
        })
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def resolve_shortcut(value: str) -> dict:
    source, resolution, fps = parse_short_config(value)
    cameras = discover_cameras(source)
    for camera in cameras:
        matching = [
            mode for mode in camera_modes(source, camera)
            if mode["resolution"] == resolution and abs(float(mode["fps"]) - fps) < 0.1
        ]
        if not matching:
            continue
        if source == "USB":
            return validate_config({
                "source": source,
                "device": camera["device"],
                "physical_path": camera.get("physical_path", ""),
                "resolution": resolution,
                "fps": fps,
            })
        raise ConfigurationError("Only USB cameras are supported.")
    raise ConfigurationError(
        f"No {source} camera currently advertises H.264 at {resolution} and {fps:g} FPS. "
        "Choose another camera or advertised mode."
    )


def _draw_frame(stdscr, title: str, subtitle: str, items: list[str], selected: int) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addnstr(0, 0, "PetCam · H.264 + MP3 HTTP", max(1, width - 1), curses.A_BOLD)
    stdscr.addnstr(2, 0, title, max(1, width - 1), curses.A_BOLD)
    if subtitle:
        stdscr.addnstr(3, 0, subtitle, max(1, width - 1))
    visible_rows = max(1, height - 7)
    first_visible = min(max(0, selected - visible_rows + 1), max(0, len(items) - visible_rows))
    for offset, item in enumerate(items[first_visible:first_visible + visible_rows]):
        index = first_visible + offset
        row = 5 + offset
        attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
        prefix = "> " if index == selected else "  "
        stdscr.addnstr(row, 0, prefix + item, max(1, width - 1), attr)
    footer = "↑/↓ Select   Enter Confirm   Esc Back"
    stdscr.addnstr(max(0, height - 1), 0, footer, max(1, width - 1), curses.A_DIM)
    stdscr.refresh()


def select_menu(
    stdscr,
    title: str,
    subtitle: str,
    items: list[str],
    *,
    refresh_signature=None,
):
    if not items:
        return None
    selected = 0
    stdscr.timeout(400)
    last_refresh = time.monotonic()
    initial_signature = refresh_signature() if refresh_signature is not None else None
    while True:
        _draw_frame(stdscr, title, subtitle, items, selected)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(items)
        elif key in (10, 13, curses.KEY_ENTER):
            return selected
        elif key == 27:
            return None
        if refresh_signature is not None and time.monotonic() - last_refresh >= 1.0:
            last_refresh = time.monotonic()
            try:
                current_signature = refresh_signature()
            except Exception:
                current_signature = "discovery-error"
            if current_signature != initial_signature:
                return HOTPLUG


def wait_for_enter(stdscr, title: str, message: str) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.timeout(-1)
    stdscr.erase()
    stdscr.addnstr(0, 0, "PetCam · H.264 + MP3 HTTP", max(1, width - 1), curses.A_BOLD)
    stdscr.addnstr(2, 0, title, max(1, width - 1), curses.A_BOLD)
    for index, line in enumerate(message.splitlines()):
        if index + 4 < height - 1:
            stdscr.addnstr(index + 4, 0, line, max(1, width - 1))
    stdscr.addnstr(height - 1, 0, "Press Enter to continue", max(1, width - 1), curses.A_DIM)
    stdscr.refresh()
    while stdscr.getch() not in (10, 13, curses.KEY_ENTER):
        pass


def discover_audio_capabilities() -> dict:
    """Query ALSA hardware constraints and retain supported PCM capture modes."""
    fallback = {"sample_rates": [48000], "channels": [1, 2]}
    if not shutil.which("arecord"):
        return fallback
    try:
        listing = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        device_lines = [line for line in listing.stdout.splitlines() if "card " in line and ", device " in line]
        device_match = next((line for line in device_lines if "kiyo" in line.lower() or "razer" in line.lower()), device_lines[0] if device_lines else "")
        if listing.returncode != 0 or not device_match:
            return fallback
        card_part = device_match.split("card ", 1)[1].split(":", 1)[0]
        device_part = device_match.split(", device ", 1)[1].split(":", 1)[0]
        device = "hw:" + card_part + "," + device_part
        result = subprocess.run(["arecord", "--dump-hw-params", "-D", device, "-d", "1", "-t", "raw", "/dev/null"], capture_output=True, text=True, timeout=5)
        lines = (result.stdout + " " + result.stderr).splitlines()
        def limits(prefix):
            line = next((item for item in lines if item.strip().startswith(prefix)), "")
            values = [int(token) for token in line.replace("[", " ").replace("]", " ").split() if token.isdigit()]
            return (min(values), max(values)) if values else None
        rate_limits = limits("RATE:")
        channel_limits = limits("CHANNELS:")
        rates = [rate for rate in (8000, 12000, 16000, 24000, 48000) if rate_limits and rate_limits[0] <= rate <= rate_limits[1]]
        channels = [count for count in (1, 2) if channel_limits and channel_limits[0] <= count <= channel_limits[1]]
        return {"sample_rates": rates or fallback["sample_rates"], "channels": channels or fallback["channels"],
                "device": "plughw:" + card_part + "," + device_part}
    except (OSError, subprocess.TimeoutExpired):
        return fallback

def configure_audio(stdscr) -> dict:
    """Use the fixed MP3 output profile and automatic ALSA selection."""
    return {"enabled": True, "sample_rate": 44100, "channels": 2, "input_format": "S16_LE", "output_codec": "MP3", "bitrate": 128000, "device": "auto"}


def guided_configuration(stdscr) -> dict:
    while True:
        source_index = select_menu(
            stdscr,
            "Step 1 · Choose capture mode",
            "Select the camera connection type.",
            ["USB"],
        )
        if source_index is None:
            raise KeyboardInterrupt
        source = "USB"

        while True:
            cameras = discover_cameras(source)
            if not cameras:
                wait_for_enter(
                    stdscr,
                    "No cameras found",
                    f"No {source} cameras are currently available.\nConnect a camera and try again.",
                )
                break

            camera_index = select_menu(
                stdscr,
                f"Step 2 · Select {source} camera",
                "Highlight the camera and press Enter.",
                [_camera_label(source, camera) for camera in cameras],
            )
            if camera_index is None:
                break
            camera = cameras[camera_index]
            modes = camera_modes(source, camera)
            if not modes:
                reason = "This camera does not advertise any native H.264 modes. Try another USB camera."
                wait_for_enter(stdscr, "No H.264 modes available", reason)
                continue

            def current_camera_signature():
                refreshed = discover_cameras(source)
                return inventory_signature(source, refreshed)

            mode_labels = [f'{mode["resolution"]}  ·  {mode["fps"]:g} FPS  ·  H.264' for mode in modes]
            mode_index = select_menu(
                stdscr,
                "Step 3 · Select H.264 mode",
                "Only resolution and frame-rate combinations reported by the camera are shown.",
                mode_labels,
                refresh_signature=current_camera_signature,
            )
            if mode_index is HOTPLUG:
                wait_for_enter(
                    stdscr,
                    "Camera list changed",
                    "The camera inventory changed while these modes were displayed.\n"
                    "The mode information may be outdated. Press Enter to return to camera selection.",
                )
                continue
            if mode_index is None:
                continue

            # Check once more before confirmation so a change immediately before Enter
            # cannot cause an obsolete camera/mode pair to be activated.
            if current_camera_signature() != inventory_signature(source, cameras):
                wait_for_enter(
                    stdscr,
                    "Camera list changed",
                    "The camera inventory changed. The displayed mode may be outdated.\n"
                    "Press Enter to return to camera selection.",
                )
                continue

            mode = modes[mode_index]
            try:
                audio = configure_audio(stdscr)
            except KeyboardInterrupt:
                continue
            audio_summary = "Audio: ALSA S16_LE -> MP3, 128 kbit/s, stereo, 44.1 kHz"
            confirm_index = select_menu(
                stdscr,
                "Step 11 · Confirm stream",
                f"{_camera_label(source, camera)}\n{mode['resolution']} at {mode['fps']:g} FPS · H.264\n"
                f"{audio_summary}",
                ["Activate /stream", "Return to camera selection"],
            )
            if confirm_index == 0:
                if source == "USB":
                    return validate_config({
                        "source": source,
                        "device": camera["device"],
                        "physical_path": camera.get("physical_path", ""),
                        "resolution": mode["resolution"],
                        "fps": mode["fps"],
                        "audio": audio,
                    })
                return validate_config({
                    "source": source,
                    "camera": camera["index"],
                    "sensor": camera.get("sensor", ""),
                    "device_path": camera.get("device_path", ""),
                    "resolution": mode["resolution"],
                    "fps": mode["fps"],
                    "audio": audio,
                })
            # Confirm's second option or Esc returns to Step 1.
            break


def _audio_capture_mode_label(audio: dict) -> str:
    """Format the configured ALSA input mode for the live status display."""
    if audio.get("enabled", True) is False:
        return "Audio: Disabled"
    sample_rate = int(audio.get("sample_rate", 48000))
    channels = int(audio.get("channels", 2))
    channel_label = {1: "Mono", 2: "Stereo"}.get(channels, f"{channels} ch")
    input_format = str(audio.get("input_format", "S16_LE"))
    return f"Audio capture mode: {sample_rate // 1000} kHz · {channel_label} · {input_format}"


def _audio_framing_label(audio: dict) -> str:
    """Describe the fixed MP3 output profile."""
    if audio.get("enabled", True) is False:
        return "Audio: Disabled"
    return "Audio output: MP3 · 128 kbit/s · Stereo · 44.1 kHz"


def _read_log(path: Path = STREAM_LOG) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _show_log_viewer(stdscr, path: Path = STREAM_LOG) -> None:
    """Display a live, scrollable tail of the VLC process log."""
    offset = 0
    follow = True
    stdscr.timeout(250)
    while True:
        lines = _read_log(path)
        height, width = stdscr.getmaxyx()
        visible = max(1, height - 4)
        maximum = max(0, len(lines) - visible)
        if follow:
            offset = maximum
        else:
            offset = min(max(offset, 0), maximum)
        stdscr.erase()
        stdscr.addnstr(0, 0, "PetCam · VLC stream log", max(1, width - 1), curses.A_BOLD)
        stdscr.addnstr(1, 0, f"{len(lines)} lines · showing {offset + 1}-{min(offset + visible, len(lines))}", max(1, width - 1), curses.A_DIM)
        for row, line in enumerate(lines[offset:offset + visible], start=2):
            stdscr.addnstr(row, 0, line, max(1, width - 1))
        stdscr.addnstr(max(0, height - 1), 0, "↑/↓ or j/k scroll · g latest · Esc/l return", max(1, width - 1), curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (27, ord("l"), ord("L"), ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            follow = False
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(maximum, offset + 1)
            follow = offset == maximum
        elif key in (curses.KEY_PPAGE,):
            follow = False
            offset = max(0, offset - visible)
        elif key in (curses.KEY_NPAGE,):
            offset = min(maximum, offset + visible)
            follow = offset == maximum
        elif key in (ord("g"), ord("G")):
            follow = True
            offset = maximum


def _read_status() -> dict:
    try:
        payload = json.loads(STREAM_STATUS.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _run_service_command(arguments: list[str]) -> None:
    command = arguments if os.geteuid() == 0 else ["sudo", *arguments]
    terminal_suspended = False
    try:
        try:
            curses.endwin()
            terminal_suspended = True
        except curses.error:
            pass
        subprocess.run(command, check=True)
    except FileNotFoundError as error:
        raise RuntimeError(f"Required command is missing: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Unable to manage petcam.service (command exited {error.returncode})."
        ) from error
    finally:
        if terminal_suspended:
            try:
                curses.reset_prog_mode()
                curses.doupdate()
            except curses.error:
                pass


def save_stream_configuration(config: dict, path: Path | None = None) -> None:
    """Persist the selected camera mode for the boot-time service."""
    target = path or CONFIG_FILE
    temporary_fd, temporary_name = tempfile.mkstemp(prefix="petcam-config-", suffix=".json")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(config, indent=2) + "\n")
        if os.geteuid() == 0:
            subprocess.run(
                ["/usr/bin/install", "-D", "-m", "0644", str(temporary), str(target)],
                check=True,
            )
        else:
            _run_service_command(
                ["/usr/bin/install", "-D", "-m", "0644", str(temporary), str(target)]
            )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"Unable to save camera configuration to {target}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)

def load_saved_stream_configuration(path: Path | None = None) -> dict | None:
    """Return the validated last-used camera configuration, if present."""
    target = path or CONFIG_FILE
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        return validate_config(value)
    except (OSError, ValueError, TypeError, ConfigurationError):
        return None


def activate_stream_service(*, restart: bool = False) -> None:
    """Enable the service at boot; restart only when applying changed settings."""
    _run_service_command(["systemctl", "enable", "petcam.service"])
    action = "restart" if restart else "start"
    _run_service_command(["systemctl", action, "petcam.service"])


def run_stream(config: dict, stdscr, *, reconfigure: bool = False) -> int:
    """Manage the persistent stream; leaving this screen never stops VLC."""
    if reconfigure:
        save_stream_configuration(config)
    activate_stream_service(restart=reconfigure)

    url = f"http://{advertised_hostname()}:{HTTP_PORT}{HTTP_PATH}"
    metrics = ResourceMetrics()
    stdscr.timeout(1000)
    start_deadline = time.monotonic() + 20
    service_seen = False
    while True:
        status = _read_status()
        status_updated_at = status.get("updated_at")
        status_stale = (
            not isinstance(status_updated_at, (int, float))
            or time.time() - status_updated_at > 5
        )
        if not status_stale and status.get("state") in {
            "ready", "starting", "reconnecting"
        }:
            service_seen = True
        if not service_seen and time.monotonic() >= start_deadline:
            raise RuntimeError(
                "petcam.service was started but has not published status. "
                f"Inspect it with: sudo journalctl -u petcam.service -n 100"
            )

        resources = metrics.sample()
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        stdscr.addnstr(0, 0, "PetCam · H.264 + MP3 HTTP", max(1, width - 1), curses.A_BOLD)
        stdscr.addnstr(2, 0, "Stream URL", max(1, width - 1), curses.A_BOLD)
        stdscr.addnstr(3, 0, url, max(1, width - 1))
        stdscr.addnstr(5, 0, "PERSISTENT STREAM SERVICE", max(1, width - 1), curses.A_BOLD)
        http_state = "STATUS STALE" if status_stale else status.get("state", "starting").upper()
        stdscr.addnstr(6, 0, f"VLC: {http_state}", max(1, width - 1))
        stdscr.addnstr(7, 0, "Transport: HTTP · MPEG-TS · H.264 + MP3", max(1, width - 1))

        stdscr.addnstr(8, 0, "Camera input: native H.264 · microphone: ALSA S16_LE", max(1, width - 1))

        stdscr.addnstr(9, 0, "VLC output: MPEG-TS · H.264 copy + MP3 128 kbit/s", max(1, width - 1))
        stdscr.addnstr(10, 0, "Audio: stereo · 44.1 kHz", max(1, width - 1))
        stdscr.addnstr(
            11, 0,
            f"Camera capture mode: {config['source']} · {config['resolution']} · {config['fps']} FPS · H.264",
            max(1, width - 1),
        )
        audio_config = config.get("audio", {})
        stdscr.addnstr(12, 0, _audio_capture_mode_label(audio_config), max(1, width - 1))
        stdscr.addnstr(13, 0, _audio_framing_label(audio_config), max(1, width - 1))
        message = str(status.get("message") or "")
        stdscr.addnstr(14, 0, message, max(1, width - 1))
        stdscr.addnstr(16, 0, "SYSTEM RESOURCES", max(1, width - 1), curses.A_BOLD)
        cpu = resources["cpu_percent"]
        cpu_text = f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "Sampling…"
        used, total = resources["memory_used_mb"], resources["memory_total_mb"]
        memory_text = (
            f"{used} / {total} MB"
            if used is not None and total is not None else "Unavailable"
        )
        stdscr.addnstr(17, 0, f"CPU: {cpu_text}", max(1, width - 1))
        stdscr.addnstr(18, 0, f"Memory: {memory_text}", max(1, width - 1))
        stdscr.addnstr(
            max(0, height - 1),
            0,
            "l: view VLC log · q: close this screen (stream keeps running)",
            max(1, width - 1),
            curses.A_DIM,
        )
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return 0
        if key in (ord("l"), ord("L")):
            _show_log_viewer(stdscr)
            stdscr.timeout(1000)

def _terminal_main(
    stdscr, config: dict | None, *, force_configure: bool = False
) -> int:
    curses.curs_set(0)
    if config is not None:
        return run_stream(config, stdscr, reconfigure=True)
    saved = None if force_configure else load_saved_stream_configuration()
    if saved is not None:
        return run_stream(saved, stdscr)
    selected = guided_configuration(stdscr)
    return run_stream(selected, stdscr, reconfigure=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="petcam",
        description="Configure a USB camera and publish one combined H.264 + MP3 stream over HTTP.",
        epilog=(
            'Examples:\n'
            '  petcam                 # show the saved stream status\n'
            '  petcam --configure     # choose a new USB camera mode\n'
            '  petcam -c "USB;1920x1080;30"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", metavar="SOURCE;RESOLUTION;FPS", dest="shortcut",
        help="apply a camera mode directly (for example: -c \"USB;1920x1080;30\")",
    )
    parser.add_argument(
        "--configure", action="store_true",
        help="run guided setup and apply a new persistent stream configuration",
    )
    args = parser.parse_args(argv)

    try:
        config = resolve_shortcut(args.shortcut) if args.shortcut else None
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("The PetCam configuration and status interface requires an interactive SSH terminal.")
        return curses.wrapper(
            _terminal_main, config, force_configure=args.configure
        )
    except (ConfigurationError, RuntimeError, OSError) as error:
        print(f"petcam: {error}", file=sys.stderr)
        return 1
    except curses.error as error:
        print(f"petcam: terminal interface failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
