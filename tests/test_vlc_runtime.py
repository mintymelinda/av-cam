from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from petcam.configuration import ConfigurationError, validate_config
from petcam.infrastructure.streaming.vlc import VlcError, build_vlc_command, load_camera_status


class VlcStreamTests(unittest.TestCase):
    def config(self):
        return {"source": "USB", "native_h264": True, "device": "/dev/video0", "resolution": "1280x720",
                "fps": 30, "audio": {"enabled": True, "device": "plughw:1,0"}}

    def test_single_stream_copies_h264_and_uses_exact_mp3_profile(self):
        command = build_vlc_command(self.config(), vlc_binary="/usr/bin/cvlc")
        sout = next(item for item in command if item.startswith("--sout="))
        self.assertNotIn("vcodec=", sout)
        self.assertEqual(sout, "--sout=#transcode{acodec=mp3,ab=128,channels=2,samplerate=44100}:std{access=http,mux=ts,dst=:8080/stream}")
        self.assertIn("access=http,mux=ts,dst=:8080/stream", sout)
        self.assertIn("--v4l2-chroma=H264", command)
        self.assertIn("v4l2:///dev/video0", command)
        self.assertIn("--input-slave=alsa://plughw:1,0", command)

    def test_rejects_non_usb_source(self):
        with self.assertRaises(VlcError):
            build_vlc_command({**self.config(), "source": "CSI"})

    def test_rejects_disabled_audio_for_combined_endpoint(self):
        with self.assertRaises(VlcError):
            build_vlc_command({**self.config(), "audio": {"enabled": False}})

    def test_saved_configuration_loads_only_usb_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.assertEqual(load_camera_status(path)["backend"], "usb")
            path.write_text(json.dumps({**self.config(), "source": "CSI"}), encoding="utf-8")
            self.assertEqual(load_camera_status(path), {})

    def test_config_validation_fixes_audio_to_mp3_profile(self):
        result = validate_config(self.config())
        self.assertEqual(result["audio"], {
            "enabled": True, "sample_rate": 44100, "channels": 2,
            "input_format": "S16_LE", "output_codec": "MP3",
            "bitrate": 128000, "device": "plughw:1,0",
        })

    def test_config_rejects_csi(self):
        with self.assertRaises(ConfigurationError):
            validate_config({**self.config(), "source": "CSI"})


if __name__ == "__main__":
    unittest.main()
