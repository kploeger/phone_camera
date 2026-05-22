from __future__ import annotations

import ssl
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


class Camera:
    """Records the MJPEG video stream from an Android IP Webcam to file.

    Frames are pulled from the phone's ``/video`` endpoint, split on JPEG
    SOI/EOI markers, and piped to a background ``ffmpeg`` process which uses
    each frame's arrival time as its PTS (variable frame rate). The resulting
    file plays back at real-time speed regardless of fluctuations in the
    phone's encoder rate.

    The phone must be running the IP Webcam app and serving on the URL passed
    to the constructor (e.g. ``http://192.168.1.42:8080``). Pass ``resolution``
    and/or ``fps`` to configure the phone before recording — both are POSTed to
    IP Webcam's settings endpoints in ``__init__``.
    """

    def __init__(
        self,
        url: str,
        *,
        fps: int = 55,
        resolution: str | None = None,
        verify_ssl: bool = False,
        ffmpeg: str = "ffmpeg",
    ):
        self.base_url = url.rstrip("/")
        self.fps = fps
        self.resolution = resolution
        self.verify_ssl = verify_ssl
        self.ffmpeg = ffmpeg

        self._proc: subprocess.Popen | None = None
        self._file: Path | None = None
        self._stream_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

        # Push requested settings to the phone. Order matters: changing
        # video_size resets the list of supported frame_durations, so set
        # resolution first, then frame_duration.
        if self.resolution is not None:
            self._post(f"/settings/video_size?set={self.resolution}")
        self._post(f"/settings/frame_duration?set={int(1e9 // self.fps)}")
        time.sleep(0.3)

    @property
    def stream_url(self) -> str:
        return f"{self.base_url}/video"

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if self.base_url.startswith("https") and not self.verify_ssl:
            return ssl._create_unverified_context()
        return None

    def _post(self, path: str) -> None:
        req = urllib.request.Request(f"{self.base_url}{path}", method="POST")
        urllib.request.urlopen(req, context=self._ssl_ctx(), timeout=5).read()

    def is_reachable(self, timeout: float = 3.0) -> bool:
        try:
            with urllib.request.urlopen(
                self.base_url, timeout=timeout, context=self._ssl_ctx()
            ) as resp:
                return 200 <= resp.status < 400
        except Exception:
            return False

    def start_recording(self, file_name: str | Path) -> None:
        if self._proc is not None:
            raise RuntimeError("already recording — call stop_recording() first")
        file_name = Path(file_name)
        file_name.parent.mkdir(parents=True, exist_ok=True)

        # image2pipe + use_wallclock_as_timestamps + vsync passthrough writes
        # VFR mp4 with PTS = wall-clock arrival time of each piped JPEG.
        self._proc = subprocess.Popen(
            [
                self.ffmpeg, "-y", "-loglevel", "error",
                "-f", "image2pipe", "-vcodec", "mjpeg",
                "-use_wallclock_as_timestamps", "1",
                "-i", "pipe:0",
                "-vsync", "passthrough",
                str(file_name),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._file = file_name

        self._stop_event = threading.Event()
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._stream_thread.start()

        # Bail early if ffmpeg couldn't open the output / start up.
        time.sleep(0.3)
        if self._proc.poll() is not None:
            err = self._proc.stderr.read().decode(errors="replace") if self._proc.stderr else ""
            self._cleanup()
            raise RuntimeError(f"ffmpeg exited immediately: {err.strip() or 'no stderr'}")

    def _stream_loop(self) -> None:
        """Pull MJPEG from /video, split on SOI/EOI, write each JPEG to ffmpeg stdin."""
        try:
            resp = urllib.request.urlopen(
                self.stream_url, context=self._ssl_ctx(), timeout=10
            )
        except Exception:
            return
        buf = b""
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = resp.read(65536)
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        break
                    jpeg = buf[s : e + 2]
                    buf = buf[e + 2 :]
                    try:
                        self._proc.stdin.write(jpeg)
                    except (BrokenPipeError, OSError):
                        return
        finally:
            try:
                resp.close()
            except Exception:
                pass
            try:
                self._proc.stdin.close()
            except Exception:
                pass

    def stop_recording(self) -> Path | None:
        if self._proc is None:
            return None
        # Tell the streaming thread to stop; it will close ffmpeg's stdin,
        # which makes ffmpeg flush the mp4 trailer and exit cleanly.
        self._stop_event.set()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=5)
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        file = self._file
        self._cleanup()
        return file

    def _cleanup(self) -> None:
        self._proc = None
        self._file = None
        self._stream_thread = None
        self._stop_event = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop_recording()
