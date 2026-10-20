#!/usr/bin/env python3
"""Small controller for the Insta360 Link webcam on macOS. Talks UVC directly, no Insta360 app needed.

  python3 link.py          open the control window (starts the server if it isn't running)
  python3 link.py serve    server only, used by the LaunchAgent
  python3 link.py check    print the current camera state (smoke test)

Requires libusb (brew install libusb). See README.md for setup and protocol details.
"""
import ctypes as C, ctypes.util, json, math, os, struct, subprocess, sys, time, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 8360
STEP = 2  # degrees per tick while an arrow is held (one tick = 150 ms in the browser)
WAKE_DELAY = 1.5  # seconds to wait after the camera turns on before moving to position 1 (it does its own wake-up move first)
VFOV = 41  # vertical field of view at 1× zoom; measured: 6° of tilt moves the person box by 17 % at 1.18×
HEAD_TOP = 0.15  # where firmware tracking puts the top of the head, so a height offset of 0° matches the original framing
SETTINGS_FILE = Path(__file__).with_name("settings.json")
VID, PID = 0x2E1A, 0x4C01
CT, XU = 1, 9  # camera terminal and Insta360 extension unit IDs from the config descriptor
PANTILT, ZOOM, MODE, PERSON = (CT, 0x0D), (CT, 0x0B), (XU, 2), (XU, 20)  # PERSON: [detected, x, y, w, h] as fractions of the frame
PAN, TILT = (-522000, 522000), (-324000, 360000)  # the camera's GET_MIN/GET_MAX, in 1/3600°
MODES = {"normal": (0, 0), "tracking": (1, 0), "whiteboard": (4, 1), "overhead": (5, 3), "deskview": (6, 0x10)}
URL = f"http://127.0.0.1:{PORT}"
ORIGINS = {URL, f"http://localhost:{PORT}"}

usb = C.CDLL(ctypes.util.find_library("usb-1.0") or "/opt/homebrew/lib/libusb-1.0.dylib")
usb.libusb_open_device_with_vid_pid.restype = C.c_void_p
usb.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_uint16, C.c_uint16]
usb.libusb_control_transfer.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8, C.c_uint16, C.c_uint16, C.c_char_p, C.c_uint16, C.c_uint]
usb.libusb_close.argtypes = [C.c_void_p]
usb.libusb_strerror.restype = C.c_char_p
usb.libusb_init(None)
handle = None
tracking = False  # our own tracking (instead of the firmware's), which allows a height offset


def xfer(rtype, req, ctrl, data):
    global handle
    if not handle:
        handle = usb.libusb_open_device_with_vid_pid(None, VID, PID)
        if not handle:
            raise OSError("Insta360 Link nicht gefunden")
    (unit, sel), buf = ctrl, C.create_string_buffer(data, len(data))
    r = usb.libusb_control_transfer(handle, rtype, req, sel << 8, unit << 8, buf, len(data), 1000)
    if r < 0:
        usb.libusb_close(handle)
        handle = None  # reopen on the next call, e.g. after unplugging
        raise OSError(usb.libusb_strerror(r).decode())
    return buf.raw[:r]


get = lambda ctrl, n: xfer(0xA1, 0x81, ctrl, bytes(n))  # GET_CUR
put = lambda ctrl, data: xfer(0x21, 0x01, ctrl, data)  # SET_CUR
clamp = lambda v, lo_hi: max(lo_hi[0], min(lo_hi[1], v))

# CoreMediaIO tells us whether any app (Zoom, Teams, a browser …) is streaming from the camera
cmio = C.CDLL("/System/Library/Frameworks/CoreMediaIO.framework/CoreMediaIO")
cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
cf.CFStringGetCString.argtypes = [C.c_void_p, C.c_char_p, C.c_long, C.c_uint32]
cf.CFRelease.argtypes = [C.c_void_p]
fourcc = lambda s: int.from_bytes(s.encode(), "big")


class Addr(C.Structure):
    _fields_ = [("sel", C.c_uint32), ("scope", C.c_uint32), ("elem", C.c_uint32)]


def cmio_prop(obj, sel, out):
    used = C.c_uint32()
    cmio.CMIOObjectGetPropertyData(obj, C.byref(Addr(fourcc(sel), fourcc("glob"), 0)), 0, None, C.sizeof(out), C.byref(used), C.byref(out))
    return used.value


# Without a run loop CMIO would never refresh its device list or state, so let it use its own thread
cmio.CMIOObjectSetPropertyData(1, C.byref(Addr(fourcc("rnlp"), fourcc("glob"), 0)), 0, None, C.sizeof(C.c_void_p), C.byref(C.c_void_p()))


def camera_in_use():
    ids = (C.c_uint32 * 32)()
    for dev in ids[: cmio_prop(1, "dev#", ids) // 4]:  # 1 = system object
        uid, buf, running = C.c_void_p(), C.create_string_buffer(256), C.c_uint32()
        cmio_prop(dev, "muid", uid)
        cf.CFStringGetCString(uid, buf, 256, 0x08000100)  # UTF-8
        cf.CFRelease(uid)
        if f"VendorID_{VID} ProductID_{PID}".encode() in buf.value:
            cmio_prop(dev, "gone", running)  # DeviceIsRunningSomewhere
            return bool(running.value)
    return False


def settings():  # positions "1"–"4" plus tracking settings (height offset and calm zone, in degrees)
    return {"offset": 0, "calm": 3, **(json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {})}


def state():
    pan, tilt = struct.unpack("<ii", get(PANTILT, 8))
    mode = get(MODE, 52)[0]
    return {"pan": pan / 3600, "tilt": tilt / 3600, "zoom": struct.unpack("<H", get(ZOOM, 2))[0],
            "mode": "tracking" if tracking else next((k for k, v in MODES.items() if v[0] == mode), None),
            "settings": settings()}


def set_mode(m):
    # Mode 0 only leaves the top layer (whiteboard falls back to overhead, for example), so retry up to 3 times
    for _ in range(3):
        put(MODE, bytes(MODES[m]) + bytes(50))
        for _ in range(30):  # 0xFF = still switching, takes up to ~1.5 s
            if (cur := get(MODE, 52)[0]) != 0xFF:
                break
            time.sleep(0.1)
        if cur == MODES[m][0]:
            return


def run(c):
    global tracking
    cmd = c["cmd"]
    if cmd in ("move", "home", "go"):
        tracking = False  # moving manually ends our own tracking
    if cmd == "move":
        pan, tilt = struct.unpack("<ii", get(PANTILT, 8))
        pan = clamp(pan + clamp(int(c["dx"]), (-1, 1)) * STEP * 3600, PAN)
        tilt = clamp(tilt + clamp(int(c["dy"]), (-1, 1)) * STEP * 3600, TILT)
        put(PANTILT, struct.pack("<ii", pan, tilt))
    elif cmd == "home":
        put(PANTILT, bytes(8))
    elif cmd == "zoom":
        put(ZOOM, struct.pack("<H", clamp(int(c["z"]), (100, 400))))
    elif cmd == "mode":
        tracking = c["m"] == "tracking"
        set_mode("normal" if tracking else c["m"])  # our tracking needs the firmware's tracking off
        if c["m"] == "normal" and "1" in settings():
            run({"cmd": "go", "i": 1})  # normal means back to the start position
    elif cmd in ("offset", "calm"):
        s = settings()
        s[cmd] = clamp(int(c["v"]), (-15, 15) if cmd == "offset" else (1, 10))
        SETTINGS_FILE.write_text(json.dumps(s))
    elif cmd == "save":
        s = settings()
        s[str(int(c["i"]))] = [*struct.unpack("<ii", get(PANTILT, 8)), *struct.unpack("<H", get(ZOOM, 2))]
        SETTINGS_FILE.write_text(json.dumps(s))
    elif cmd == "go":
        pan, tilt, zoom = settings()[str(int(c["i"]))]
        put(PANTILT, struct.pack("<ii", pan, tilt))
        put(ZOOM, struct.pack("<H", zoom))
    else:
        raise KeyError(cmd)


class Server(HTTPServer):
    was_in_use = True  # don't jump on startup if a call is already running
    next_follow, last_box = 0, None

    def service_actions(self):  # runs between requests, at least every 0.5 s
        in_use = camera_in_use()
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
        b = get(PERSON, 17)
        if not b[0] or b == self.last_box:
            return False  # nobody detected, or the box is still the one from before the last move
        x, y, w, _ = struct.unpack("<4f", b[1:])
        vfov = 2 * math.degrees(math.atan(math.tan(math.radians(VFOV / 2)) * 100 / struct.unpack("<H", get(ZOOM, 2))[0]))
        hfov = 2 * math.degrees(math.atan(16 / 9 * math.tan(math.radians(vfov / 2))))
        dpan = (x + w / 2 - 0.5) * hfov
        dtilt = (HEAD_TOP - y) * vfov + s["offset"]  # positive = up
        dpan, dtilt = (dpan if abs(dpan) > s["calm"] else 0), (dtilt if abs(dtilt) > s["calm"] else 0)  # calm zone
        if not (dpan or dtilt):
            return False
        pan, tilt = struct.unpack("<ii", get(PANTILT, 8))
        put(PANTILT, struct.pack("<ii", clamp(pan + int(dpan * 0.7 * 3600), PAN), clamp(tilt + int(dtilt * 0.7 * 3600), TILT)))
        self.last_box = b
        return True


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def handle_api(self, fn):
        try:
            self.reply(200, fn())
        except OSError as e:
            self.reply(503, {"error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            self.reply(400, {"error": f"Ungültiger Befehl: {e}"})

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        self.handle_api(state)

    def do_POST(self):
        # Only our own page may control the camera: blocks other websites and DNS rebinding
        if self.headers.get("Origin") not in ORIGINS:
            return self.reply(403, {"error": "forbidden"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.handle_api(lambda: (run(body), state())[1])

    def log_message(self, *_):
        pass


PAGE = """<!doctype html><html lang=de><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Insta360 Link</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f4;--card:#fff;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--acc:#ea580c}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--card:#292524;--fg:#f5f5f4;--mut:#a8a29e;--line:#44403c}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.3 -apple-system,system-ui,sans-serif;
  display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:20px;align-items:start}
@media (max-width:640px){body{grid-template-columns:1fr}}
video{display:block;width:100%;aspect-ratio:16/9;background:#000;border-radius:12px}
.ctl{display:grid;gap:16px}
h2{margin:0 0 6px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.row{display:flex;flex-wrap:wrap;gap:6px}
button{flex:1 0 28%;padding:9px 0;border:1px solid var(--line);border-radius:8px;background:var(--card);color:inherit;font:inherit;cursor:pointer;touch-action:none;user-select:none}
button.on{background:var(--acc);border-color:var(--acc);color:#fff}
button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.pad{display:grid;grid-template-columns:repeat(3,52px);gap:6px;justify-content:center}
.pad button{height:52px;font-size:18px}
.slider{display:flex;gap:10px;align-items:center}
.slider+.slider{margin-top:4px}
.lbl{width:34px;color:var(--mut);font-size:12px}
input[type=range]{flex:1;accent-color:var(--acc)}
small,#st{display:block;margin-top:8px;color:var(--mut);font-size:12px}
</style>
<main><video id=cam autoplay muted playsinline></video><div id=st></div></main>
<div class=ctl>
<section><h2>Modus</h2><div class=row>
<button data-mode=normal>Normal</button><button data-mode=tracking>Tracking</button><button data-mode=deskview>DeskView</button>
<button data-mode=whiteboard>Whiteboard</button><button data-mode=overhead>Overhead</button></div></section>
<section><h2>Tracking</h2>
<div class=slider><span class=lbl>Höhe</span><input id=offset type=range min=-10 max=10 aria-label=Tracking-Höhe><span id=offset-l></span></div>
<div class=slider><span class=lbl>Ruhe</span><input id=calm type=range min=1 max=10 aria-label=Ruhezone><span id=calm-l></span></div>
<small>Höhe +: Kamera zielt höher, weniger Brust im Bild. Ruhe: so viele Grad darfst du dich bewegen, bevor sie nachführt.</small></section>
<section><h2>Schwenken</h2><div class=pad>
<span></span><button data-dx=0 data-dy=1 aria-label=hoch>▲</button><span></span>
<button data-dx=-1 data-dy=0 aria-label=links>◀</button><button data-home aria-label=Mitte>⌂</button><button data-dx=1 data-dy=0 aria-label=rechts>▶</button>
<span></span><button data-dx=0 data-dy=-1 aria-label=runter>▼</button><span></span></div></section>
<section><h2>Zoom</h2><div class=slider><input id=zoom type=range min=100 max=400 aria-label=Zoom><span id=zl></span></div></section>
<section><h2>Positionen</h2><div class=row>
<button data-preset=1>★ 1</button><button data-preset=2>2</button><button data-preset=3>3</button><button data-preset=4>4</button></div>
<small>Klick fährt an, ⇧-Klick speichert. ★ 1 wird automatisch angefahren, sobald eine App die Kamera einschaltet.</small></section>
</div>
<script>
const $ = s => document.querySelectorAll(s), el = id => document.getElementById(id);
let hold;
async function api(body) {
  try {
    const r = await fetch(body ? '/api' : '/state', body ? {method: 'POST', body: JSON.stringify(body)} : {});
    const j = await r.json();
    if (!r.ok) throw Error(j.error);
    $('[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode == j.mode));
    $('[data-preset]').forEach(b => b.style.opacity = j.settings[b.dataset.preset] ? 1 : .45);
    for (const k of ['offset', 'calm']) {
      if (document.activeElement != el(k)) el(k).value = j.settings[k];
      el(k + '-l').textContent = (k == 'offset' && j.settings[k] > 0 ? '+' : '') + j.settings[k] + '°';
    }
    if (document.activeElement != el('zoom')) el('zoom').value = j.zoom;
    el('zl').textContent = (j.zoom / 100).toFixed(1) + '×';
    el('st').textContent = `Pan ${j.pan.toFixed(1)}° · Tilt ${j.tilt.toFixed(1)}°`;
  } catch (e) { el('st').textContent = '⚠ ' + e.message }
}
async function preview() {
  const md = navigator.mediaDevices;
  const find = async () => (await md.enumerateDevices()).find(d => d.kind == 'videoinput' && /insta360 link/i.test(d.label));
  try {
    let cam = await find();
    if (!cam) { (await md.getUserMedia({video: true})).getTracks().forEach(t => t.stop()); cam = await find() }  // device names only show up after camera permission
    if (!cam) throw Error('Insta360 Link nicht gefunden');
    el('cam').srcObject = await md.getUserMedia({video: {deviceId: {exact: cam.deviceId}, width: {ideal: 1280}}});
  } catch (e) { el('st').textContent = '⚠ Vorschau: ' + e.message }
}
document.onclick = e => {
  const d = e.target.dataset || {};
  if (d.mode) api({cmd: 'mode', m: d.mode});
  if (d.preset) api({cmd: e.shiftKey ? 'save' : 'go', i: +d.preset});
  if ('home' in d) api({cmd: 'home'});
};
const stop = () => clearInterval(hold);
$('[data-dx]').forEach(b => {
  b.onpointerdown = () => { stop(); const go = () => api({cmd: 'move', dx: +b.dataset.dx, dy: +b.dataset.dy}); go(); hold = setInterval(go, 150) };
  b.onpointerup = b.onpointerleave = b.onpointercancel = stop;
});
window.onblur = stop;
el('zoom').oninput = () => api({cmd: 'zoom', z: +el('zoom').value});
['offset', 'calm'].forEach(k => el(k).onchange = () => api({cmd: k, v: +el(k).value}));
setInterval(api, 3000);  // hand gestures change the camera state too
api();
preview();
</script></html>"""

if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(print(json.dumps({**state(), "in_use": camera_in_use()}, indent=2)))
    if sys.argv[1:] != ["serve"]:
        chrome = "/Applications/Google Chrome.app"
        if os.path.exists(chrome):  # slim app window without tabs or address bar
            subprocess.Popen(["open", "-na", chrome, "--args", f"--app={URL}", "--window-size=1000,640"])
        else:
            webbrowser.open(URL)
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is busy, the controller is probably running already.")
    print(f"Insta360 Link controller running on {URL}, Ctrl+C to stop.", file=sys.stderr)
    server.serve_forever()
