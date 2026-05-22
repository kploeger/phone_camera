"""Record a 5-second clip from the IP Webcam phone."""

import time

from phone_camera import Camera

URL = "https://172.16.9.183:8080"


def main() -> None:
    cam = Camera(URL, verify_ssl=False)
    if not cam.is_reachable():
        raise SystemExit(f"phone not reachable at {URL}")

    cam.start_recording("/tmp/phone_clip.mp4")
    print("recording...")
    time.sleep(5)
    out = cam.stop_recording()
    print(f"saved to {out}")


if __name__ == "__main__":
    main()
