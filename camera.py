"""Insta360 Link hardware access: UVC control requests over libusb, plus CoreMediaIO to see whether an app is streaming."""
import ctypes as C, ctypes.util, struct, time

VID, PID = 0x2E1A, 0x4C01
CT, XU = 1, 9  # camera terminal and Insta360 extension unit IDs from the config descriptor
PANTILT, ZOOM, MODE, PERSON, OUTPUT = (CT, 0x0D), (CT, 0x0B), (XU, 2), (XU, 20), (XU, 28)
PAN, TILT = (-522000, 522000), (-324000, 360000)  # the camera's GET_MIN/GET_MAX, in 1/3600°
MODES = {"normal": (0, 0), "tracking": (1, 0), "whiteboard": (4, 1), "overhead": (5, 3), "deskview": (6, 0x10)}

usb = C.CDLL(ctypes.util.find_library("usb-1.0") or "/opt/homebrew/lib/libusb-1.0.dylib")
usb.libusb_open_device_with_vid_pid.restype = C.c_void_p
usb.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_uint16, C.c_uint16]
usb.libusb_control_transfer.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8, C.c_uint16, C.c_uint16, C.c_char_p, C.c_uint16, C.c_uint]
usb.libusb_close.argtypes = [C.c_void_p]
usb.libusb_strerror.restype = C.c_char_p
usb.libusb_init(None)
handle = None


def xfer(rtype, req, ctrl, data):
    global handle
    if not handle:
        handle = usb.libusb_open_device_with_vid_pid(None, VID, PID)
        if not handle:
            raise OSError("Insta360 Link not found")
    (unit, sel), buf = ctrl, C.create_string_buffer(data, len(data))
    r = usb.libusb_control_transfer(handle, rtype, req, sel << 8, unit << 8, buf, len(data), 1000)
    if r < 0:
        usb.libusb_close(handle)
        handle = None  # reopen on the next call, e.g. after unplugging
        raise OSError(usb.libusb_strerror(r).decode())
    return buf.raw[:r]


get = lambda ctrl, n: xfer(0xA1, 0x81, ctrl, bytes(n))  # GET_CUR
put = lambda ctrl, data: xfer(0x21, 0x01, ctrl, data)  # SET_CUR
clamp = lambda v, lo, hi: max(lo, min(hi, v))


def pantilt():  # (pan, tilt) in 1/3600°
    return struct.unpack("<ii", get(PANTILT, 8))


def set_pantilt(pan, tilt):
    put(PANTILT, struct.pack("<ii", clamp(int(pan), *PAN), clamp(int(tilt), *TILT)))


def zoom():  # 100–400
    return struct.unpack("<H", get(ZOOM, 2))[0]


def set_zoom(z):
    put(ZOOM, struct.pack("<H", clamp(int(z), 100, 400)))


def mode():  # firmware mode name, None while switching
    m = get(MODE, 52)[0]
    return next((k for k, v in MODES.items() if v[0] == m), None)


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


def person():  # (x, y, w, h) of the detected person as fractions of the frame, or None
    b = get(PERSON, 17)
    return struct.unpack("<4f", b[1:]) if b[0] else None


def output():  # (width, height, fps) the camera is sending; read-only, the streaming apps negotiate it
    return struct.unpack("<IIH", get(OUTPUT, 10))


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


def in_use():
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
