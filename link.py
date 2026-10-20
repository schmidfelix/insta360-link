#!/usr/bin/env python3
"""Link Control: a small local controller for the Insta360 Link webcam on macOS.

  python3 link.py          open the control window (starts the server if it isn't running)
  python3 link.py serve    server only, used by the LaunchAgent
  python3 link.py check    print the current camera state (smoke test)

Requires libusb (brew install libusb). See README.md for setup and protocol details.
"""
import json, math, os, subprocess, sys, time, webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import camera as cam

PORT = 8360
STEP = 2  # degrees per tick while an arrow is held (one tick = 150 ms in the browser)
WAKE_DELAY = 1.5  # seconds to wait after the camera turns on before moving to position 1 (it does its own wake-up move first)
VFOV = 41  # vertical field of view at 1× zoom; measured: 6° of tilt moves the person box by 17 % at 1.18×
HEAD_TOP = 0.15  # where firmware tracking puts the top of the head, so a height offset of 0° matches the original framing
HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.json"
URL = f"http://127.0.0.1:{PORT}"
ORIGINS = {URL, f"http://localhost:{PORT}"}
tracking = False  # our own tracking (instead of the firmware's), which allows a height offset


def settings():  # positions "1"–"4" plus tracking settings (height offset and calm zone, in degrees)
    return {"offset": 0, "calm": 3, **(json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {})}


def state():
    pan, tilt = cam.pantilt()
    return {"pan": pan / 3600, "tilt": tilt / 3600, "zoom": cam.zoom(),
            "mode": "tracking" if tracking else cam.mode(), "person": cam.person(),
            "output": cam.output() if cam.in_use() else None, "settings": settings()}


def run(c):
    global tracking
    cmd = c["cmd"]
    if cmd in ("move", "home", "go"):
        tracking = False  # moving manually ends our own tracking
    if cmd == "move":
        pan, tilt = cam.pantilt()
        step = STEP * 3600
        cam.set_pantilt(pan + cam.clamp(int(c["dx"]), -1, 1) * step, tilt + cam.clamp(int(c["dy"]), -1, 1) * step)
    elif cmd == "home":
        cam.set_pantilt(0, 0)
    elif cmd == "zoom":
        cam.set_zoom(c["z"])
    elif cmd == "mode":
        tracking = c["m"] == "tracking"
        cam.set_mode("normal" if tracking else c["m"])  # our tracking needs the firmware's tracking off
        if c["m"] == "normal" and "1" in settings():
            run({"cmd": "go", "i": 1})  # normal means back to the start position
    elif cmd in ("offset", "calm"):
        s = settings()
        s[cmd] = cam.clamp(int(c["v"]), *((-15, 15) if cmd == "offset" else (1, 10)))
        SETTINGS_FILE.write_text(json.dumps(s))
    elif cmd == "save":
        s = settings()
        s[str(cam.clamp(int(c["i"]), 1, 4))] = [*cam.pantilt(), cam.zoom()]
        SETTINGS_FILE.write_text(json.dumps(s))
    elif cmd == "go":
        pan, tilt, zoom = settings()[str(int(c["i"]))]
        cam.set_pantilt(pan, tilt)
        cam.set_zoom(zoom)
    else:
        raise KeyError(cmd)


class Server(HTTPServer):
    was_in_use = True  # don't jump on startup if a call is already running
    next_follow, last_box = 0, None

    def service_actions(self):  # runs between requests, at least every 0.5 s
        in_use = cam.in_use()
        if in_use and not self.was_in_use and "1" in settings():
            time.sleep(WAKE_DELAY)
            try:
                run({"cmd": "go", "i": 1})
            except OSError as e:
                print("Moving to start position failed:", e, file=sys.stderr)
        self.was_in_use = in_use
        if tracking and in_use and time.time() > self.next_follow:
            try:
                moved = self.follow(settings())
            except OSError as e:
                moved = False
                print("Tracking failed:", e, file=sys.stderr)
            self.next_follow = time.time() + (1.5 if moved else 0.5)  # after a move, let the gimbal and detection catch up

    def follow(self, s):
        # ponytail: stepwise follow instead of the firmware's control loop; slower to react, but never jittery
        box = cam.person()
        if not box or box == self.last_box:
            return False  # nobody detected, or the box is still the one from before the last move
        x, y, w, _ = box
        vfov = 2 * math.degrees(math.atan(math.tan(math.radians(VFOV / 2)) * 100 / cam.zoom()))
        hfov = 2 * math.degrees(math.atan(16 / 9 * math.tan(math.radians(vfov / 2))))
        dpan = (x + w / 2 - 0.5) * hfov
        dtilt = (HEAD_TOP - y) * vfov + s["offset"]  # positive = up
        dpan, dtilt = (dpan if abs(dpan) > s["calm"] else 0), (dtilt if abs(dtilt) > s["calm"] else 0)  # calm zone
        if not (dpan or dtilt):
            return False
        pan, tilt = cam.pantilt()
        cam.set_pantilt(pan + dpan * 0.7 * 3600, tilt + dtilt * 0.7 * 3600)
        self.last_box = box
        return True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE / "web"), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")  # always pick up a fresh UI after an update
        super().end_headers()

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def handle_api(self, fn):
        try:
            self.reply(200, fn())
        except OSError as e:
            self.reply(503, {"error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            self.reply(400, {"error": f"Invalid command: {e}"})

    def do_GET(self):
        if self.path == "/state":
            return self.handle_api(state)
        super().do_GET()

    def do_POST(self):
        if self.path != "/api":
            return self.reply(404, {"error": "not found"})
        # Only our own page may control the camera: blocks other websites and DNS rebinding
        if self.headers.get("Origin") not in ORIGINS:
            return self.reply(403, {"error": "forbidden"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.handle_api(lambda: (run(body), state())[1])

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(print(json.dumps({**state(), "in_use": cam.in_use()}, indent=2)))
    if sys.argv[1:] != ["serve"]:
        chrome = "/Applications/Google Chrome.app"
        if os.path.exists(chrome):  # slim app window without tabs or address bar
            subprocess.Popen(["open", "-na", chrome, "--args", f"--app={URL}", "--window-size=1240,880"])
        else:
            webbrowser.open(URL)
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is busy, the controller is probably running already.")
    print(f"Link Control running on {URL}, Ctrl+C to stop.", file=sys.stderr)
    server.serve_forever()
