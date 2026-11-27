"""Discover USB cameras and their native H.264 capture modes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...domain.camera.capabilities import parse_v4l2_modes


def _usb_parent(video_entry: Path) -> Path | None:
    try:
        device_path = (video_entry / "device").resolve(strict=True)
    except OSError:
        return None
    for parent in (device_path, *device_path.parents):
        if (parent / "idVendor").is_file() and (parent / "idProduct").is_file():
            return parent
    return None


def discover_usb_cameras(
    sysfs_root: Path = Path("/sys/class/video4linux"),
    dev_root: Path = Path("/dev"),
) -> list[dict]:
    """Return one V4L2 capture node per physical USB camera, including its modes."""
    groups: dict[str, list[Path]] = {}
    for entry in sorted(sysfs_root.glob("video*")):
        parent = _usb_parent(entry)
        node = dev_root / entry.name
        if parent is not None and node.exists():
            groups.setdefault(str(parent), []).append(node)

    aliases: dict[str, Path] = {}
    for alias in sorted((dev_root / "v4l/by-id").glob("*")):
        try:
            aliases[str(alias.resolve(strict=True))] = alias
        except OSError:
            continue

    cameras = []
    for physical_path, nodes in sorted(groups.items()):
        candidates = sorted(nodes, key=lambda node: (str(node) not in aliases, str(node)))
        for node in candidates:
            device = str(aliases.get(str(node), node))
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "--device", device, "--list-formats-ext"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode:
                continue
            modes = parse_v4l2_modes(result.stdout)
            if not modes:
                continue
            name_path = sysfs_root / node.name / "name"
            try:
                label = name_path.read_text(encoding="utf-8").strip()
            except OSError:
                label = "USB camera"
            cameras.append({
                "kind": "USB",
                "device": device,
                "physical_path": physical_path,
                "label": label or "USB camera",
                "modes": modes,
            })
            break
    return cameras


