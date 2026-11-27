# PetCam

PetCam publishes one combined USB-camera and microphone stream using VLC's built-in
HTTP output. The camera's native H.264 video is copied unchanged; audio is captured
from ALSA as signed 16-bit PCM and encoded using the fixed MP3 profile below.

## Supported platform

- Raspberry Pi OS Trixie Lite, 32-bit ARMHF userspace
- USB cameras that expose native H.264 through V4L2
- ALSA microphone capture device

CSI cameras and software video encoding are not supported.

## Stream format

The single endpoint is:

`http://DEVICE-HOSTNAME.local:8080/stream`

It carries one MPEG-TS stream containing:

- Video: native camera H.264, VLC `vcodec=copy` (no video re-encoding)
- Audio: MP3, 128 kbit/s, stereo, 44.1 kHz
- Transport: VLC built-in HTTP output; no Live555, custom RTSP server, or RTSP session

The VLC transcode profile is exactly:

`#transcode{acodec=mp3,ab=128,channels=2,samplerate=44100}`

No video codec conversion is requested, so VLC passes the camera H.264 video through
unchanged without software encoding.

## Install

On Raspberry Pi OS:

```sh
sudo ./install.sh
petcam
```

The installer installs VLC, V4L2 and ALSA utilities, configures the persistent
`petcam.service`, and installs the CLI. On first run, select the USB camera and one
of its advertised native H.264 modes. PetCam saves that selection and starts the
stream service. The stream continues after the terminal status screen is closed.

The ALSA device defaults to automatic selection, preferring a Razer Kiyo microphone
when present and otherwise selecting the first capture device. Set
`PETCAM_AUDIO_DEVICE=plughw:CARD,DEVICE` in the service environment to pin a device.

Useful service commands:

```sh
sudo systemctl status petcam.service
sudo journalctl -u petcam.service -f
sudo systemctl restart petcam.service
```

To change the selected camera mode:

```sh
petcam --configure
petcam -c "USB;1920x1080;30"
```

The hostname in the displayed URL uses mDNS via `avahi-daemon`. Set
`PETCAM_ADVERTISE_HOST` when using a different DNS name.

## Repair an installed image

The prebuilt image retains its release checkout at `/opt/petcam/source/`:

```sh
sudo /opt/petcam/source/install.sh
```

## Build an image locally

```sh
sudo ./build-image.sh
```

## Create a release

```sh
sudo ./release.sh v1.0.0
```

Pushing a `v*` tag runs the release workflow and publishes the compressed image,
checksum, and Raspberry Pi Imager manifest to the matching GitHub Release.
