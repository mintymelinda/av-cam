"""Run VLC as the single-process H.264 + MP3 HTTP stream publisher."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import signal
import subprocess
import time
from pathlib import Path

RUN_DIR = Path(os.environ.get("PETCAM_RUN", "/run/petcam"))
STREAM_STATUS = RUN_DIR / "stream-status.json"
CONFIG_FILE = Path(os.environ.get("PETCAM_CONFIG", "/etc/petcam/config.json"))
HTTP_PORT = 8080
HTTP_PATH = "/stream"


class VlcError(RuntimeError):
    """Raised when VLC cannot configure or run the combined stream."""


def load_camera_status(path: Path | str = CONFIG_FILE) -> dict:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(config, dict) or str(config.get("source", "")).lower() != "usb":
            return {}
        width_text, height_text = str(config.get("resolution", "")).split("x", 1)
        width, height = int(width_text), int(height_text)
        fps = float(config.get("fps"))
        device = str(config.get("device", ""))
        if not (0 < width <= 65535 and 0 < height <= 65535 and 0 < fps <= 1000):
            return {}
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)", device):
            return {}
        audio = config.get("audio", {})
        if not isinstance(audio, dict):
            audio = {}
        return {
            **config, "source": "USB", "backend": "usb", "native_h264": True,
            "resolution": f"{width}x{height}", "fps": fps, "device": device,
            "audio": audio,
        }
    except (OSError, ValueError, TypeError):
        return {}


def detect_audio_device(config: dict) -> str:
    audio = config.get("audio", {})
    environment_device = os.environ.get("PETCAM_AUDIO_DEVICE")
    configured = str(environment_device if environment_device else audio.get("device") or "auto").strip()
    if configured.lower() != "auto":
        if not configured or any(c in configured for c in "\r\n"):
            raise VlcError("The ALSA capture device name is invalid.")
        return configured
    arecord = shutil.which("arecord")
    if not arecord:
        raise VlcError("arecord is missing; install alsa-utils.")
    try:
        result = subprocess.run([arecord, "-l"], check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VlcError(f"Unable to discover ALSA capture devices: {error}") from error
    if result.returncode:
        raise VlcError((result.stderr or result.stdout or "Unable to list ALSA capture devices.").strip())
    devices = []
    for line in result.stdout.splitlines():
        match = re.search(r"^\s*card\s+(\d+):.*?,\s*device\s+(\d+):", line)
        if match:
            devices.append((int(match.group(1)), int(match.group(2)), line))
    if not devices:
        raise VlcError("No ALSA capture device found. Connect the microphone or set PETCAM_AUDIO_DEVICE.")
    selected = next((item for item in devices if "kiyo" in item[2].lower() or "razer" in item[2].lower()), devices[0])
    return f"plughw:{selected[0]},{selected[1]}"


def build_vlc_command(config: dict, *, vlc_binary: str = "cvlc") -> list[str]:
    if config.get("source") != "USB" or config.get("native_h264") is not True:
        raise VlcError("Only native USB H.264 camera input is supported.")
    device = str(config.get("device", ""))
    if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)", device):
        raise VlcError("Configured USB camera device path is invalid.")
    try:
        width, height = (int(part) for part in str(config["resolution"]).split("x", 1))
        fps = float(config["fps"])
    except (KeyError, TypeError, ValueError) as error:
        raise VlcError("Configured camera mode is invalid.") from error
    if not (0 < width <= 65535 and 0 < height <= 65535 and 0 < fps <= 1000):
        raise VlcError("Configured camera mode is outside supported limits.")
    audio = config.get("audio", {})
    if audio.get("enabled", True) is False:
        raise VlcError("The combined /stream endpoint requires microphone audio to be enabled.")
    alsa_device = detect_audio_device(config)
    if not re.fullmatch(r"(?:plug)?hw:[A-Za-z0-9_.-]+,\d+", alsa_device):
        raise VlcError("Configured ALSA device must be a hw:CARD,DEVICE or plughw:CARD,DEVICE name.")
    sout = (
        "#transcode{acodec=mp3,ab=128,channels=2,samplerate=44100}"
        ":std{access=http,mux=ts,dst=:8080/stream}"
    )
    return [
        vlc_binary, "--intf", "dummy", "--no-video-title-show", "--no-sout-display",
        "--sout-keep", f"--sout={sout}",
        f"--input-slave=alsa://{alsa_device}",
        "--v4l2-chroma=H264", f"--v4l2-width={width}",
        f"--v4l2-height={height}", f"--v4l2-fps={fps:g}",
        f"v4l2://{device}",
    ]


def write_status(state: str, source: dict, message: str = "") -> None:
    RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    payload = {
        "state": state, "source": source.get("backend"),
        "resolution": source.get("resolution"), "capture_fps": source.get("fps"),
        "stream_url": f"http://{os.environ.get('PETCAM_ADVERTISE_HOST', 'localhost')}:{HTTP_PORT}{HTTP_PATH}",
        "message": message, "updated_at": time.time(),
    }
    temporary = STREAM_STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, STREAM_STATUS)


def run_server() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_camera_status()
    if not config:
        logging.error("No valid saved USB H.264 camera configuration at %s", CONFIG_FILE)
        return 2
    binary = shutil.which("cvlc")
    if not binary:
        logging.error("cvlc is missing; install VLC.")
        return 2
    try:
        command = build_vlc_command(config, vlc_binary=binary)
    except VlcError as error:
        logging.error("%s", error)
        return 2
    process = None
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    write_status("starting", config)
    logging.info("Starting VLC combined H.264 + MP3 stream at http://0.0.0.0:%d%s", HTTP_PORT, HTTP_PATH)
    logging.info("VLC command: %s", " ".join(command))
    try:
        process = subprocess.Popen(command)
        start_deadline = time.monotonic() + 20
        while process.poll() is None and not stopping:
            try:
                with socket.create_connection(("127.0.0.1", HTTP_PORT), timeout=0.5):
                    ready = True
            except OSError:
                ready = False
            write_status("ready" if ready else "starting", config)
            if not ready and time.monotonic() >= start_deadline:
                logging.error("VLC did not open the HTTP stream listener within 20 seconds.")
                process.terminate()
                return 1
            time.sleep(1)
        return process.wait()
    except OSError as error:
        logging.exception("Unable to start VLC: %s", error)
        write_status("error", config, str(error))
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        write_status("stopped", config)


if __name__ == "__main__":
    raise SystemExit(run_server())
