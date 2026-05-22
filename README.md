# Phone Camera

Tiny wrapper around the Android [IP Webcam](https://play.google.com/store/apps/details?id=com.pas.webcam) app: connect to the phone's URL, then `start_recording(path)` / `stop_recording()`. A background thread parses the MJPEG multipart stream from `<url>/video` and pipes JPEGs into `ffmpeg`, which writes a variable-frame-rate mp4 timestamped by wall-clock arrival — so playback runs at real-time speed even when the phone's encoder rate fluctuates.

## Requirements

- `ffmpeg` on `PATH`.
- The IP Webcam app running on the phone, reachable from the host (same Wi-Fi, USB tethering, hotspot, etc.).

## Usage

```python
import time
from phone_camera import Camera

cam = Camera(
    "https://10.50.171.68:8080",
    fps=55,                     # default; phone caps at ~57 fps in practice
    resolution="1920x1080",     # optional; None leaves the phone unchanged
    verify_ssl=False,           # IP Webcam uses a self-signed cert
)
assert cam.is_reachable()

cam.start_recording("/tmp/clip.mp4")
time.sleep(5)
cam.stop_recording()
```

`Camera` is also a context manager — `stop_recording()` runs on exit.

## Notes & known limitations

- **The constructor mutates the phone**: it POSTs the requested `resolution` and `fps` to IP Webcam's `/settings/...` endpoints. These persist until changed again.
- **`fps` is a phone-side target**, not an exact output rate. The output is VFR — each frame's PTS comes from its wall-clock arrival time, so the file always plays at real-time speed regardless of the actual delivered rate.
- **No audio.** Video only.
- **No mid-recording error recovery.** If the phone disconnects partway through, the file is finalized with whatever frames arrived; `stop_recording()` returns normally without raising.
- **Sustained delivery rates** measured on a Pixel 9 Pro over USB tether:

  | resolution | sustained fps |
  |---|---|
  | 3840×2160 (4K) | ~40 |
  | 2688×1512 | ~57 |
  | 1920×1080 | ~58 |

  Requesting higher `fps` than the phone can sustain doesn't break anything — you just get the actual rate.
