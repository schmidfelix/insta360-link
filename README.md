# Insta360 Link Control

A tiny macOS replacement for the official Insta360 Link Controller app. It's a single Python file with no pip packages. You get a small control window with a live preview, plus a background service that puts the camera where you want it every time an app turns it on.

The window's labels are in German.

## Features

- **Live preview** with pan/tilt (press and hold the arrows), zoom and a home button
- **Modes:** Normal, Tracking, DeskView, Whiteboard, Overhead
- **4 saved positions:** click to recall, ⇧-click to save the current one
- **Start position:** position ★ 1 is applied automatically whenever any app (Zoom, Teams, a browser …) starts using the camera, and whenever you switch back to *Normal*
- **Tracking with a height offset:** follows you using the camera's own person detection. Two sliders: *Höhe* (height offset in degrees; positive aims higher) and *Ruhe* (calm zone: how many degrees you can move before the camera follows). Tracking never touches the zoom and doesn't jitter.

## Requirements

- macOS (tested on macOS 27 on Apple Silicon)
- Insta360 **Link** (USB ID `2e1a:4c01`). The Link 2 uses a different ID and is untested.
- Python 3: `/usr/bin/python3` from the Xcode Command Line Tools works
- libusb: `brew install libusb`
- Google Chrome (optional): opens the controls in a slim app window. Without Chrome, the default browser is used.

You don't need the official Insta360 app. You can uninstall it.

## Install

```sh
git clone https://github.com/schmidfelix/insta360-link.git
cd insta360-link
brew install libusb
python3 link.py check   # prints pan/tilt/zoom, so you know the camera is reachable
python3 link.py         # starts the server and opens the control window
```

The first time you open the window, Chrome asks for camera permission for the preview. Allow it once.

## Autostart

The start position and tracking both run inside the server, so it should always be running. To start it at login, install it as a LaunchAgent. Run these commands **from the repo directory** (`$PWD` gets baked into the plist):

```sh
cat > ~/Library/LaunchAgents/local.insta360-link.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.insta360-link</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$PWD/link.py</string><string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/insta360-link.log</string>
</dict></plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.insta360-link.plist
```

After that, the server starts at login and restarts if it crashes.

| Task | Command |
|---|---|
| Open the window | `python3 link.py`, or http://127.0.0.1:8360 in any browser |
| Restart (e.g. after `git pull`) | `launchctl kickstart -k gui/$(id -u)/local.insta360-link` |
| Logs | `~/Library/Logs/insta360-link.log` |
| Uninstall | `launchctl bootout gui/$(id -u)/local.insta360-link && rm ~/Library/LaunchAgents/local.insta360-link.plist` |

### Spotlight launcher (optional)

This creates an app named "Insta360 Link" that opens the control window:

```sh
mkdir -p ~/Applications
osacompile -o ~/Applications/"Insta360 Link.app" \
  -e 'do shell script "open -na \"Google Chrome\" --args --app=http://127.0.0.1:8360 --window-size=1000,640"'
```

## Usage notes

- Positions and tracking settings are stored in `settings.json` next to `link.py`. Git ignores this file.
- If you start tracking with the **palm gesture**, the camera runs its own firmware tracking, which has no height offset. Click *Tracking* in the window to switch to the offset-aware tracking.
- A few constants at the top of `link.py` are meant for tuning:
  - `WAKE_DELAY`: increase it if the camera ends up somewhere other than position 1 after turning on.
  - `STEP`: arrow speed.
  - `VFOV` and `HEAD_TOP`: tracking geometry.

## How it works

The Link is a standard UVC camera with three vendor extension units. `link.py` sends UVC control requests through libusb (via `ctypes`). macOS's own driver keeps streaming at the same time, so this needs no root and no kernel extension, and it works during calls.

**Standard UVC** (camera terminal, unit 1):

| Selector | Control | Notes |
|---|---|---|
| `0x0D` | PanTilt Absolute | 2 × int32, in 1/3600°. Pan ±145°, tilt −90° … +100° |
| `0x0B` | Zoom Absolute | uint16, 100–400 |
| `0x0E` | PanTilt Relative | rejected by the camera, so holding an arrow sends small absolute steps instead |

**Insta360 extension unit 9** (GUID `faf1672d-b71b-4793-8c91-7b1c9b7f95f8`):

| Selector | Size | Meaning |
|---|---|---|
| 2 | 52 B | Mode: byte 0 = mode, byte 1 = flag. Normal `00 00`, tracking `01 00`, whiteboard `04 01`, overhead `05 03`, DeskView `06 10`. Reads `FF` while switching. Writing `00` only leaves the top layer (whiteboard → overhead), so the script repeats it until the camera reads back `00`. |
| 19 | 1 B | Firmware tracking framing: 1 head, 2 half body, 3 full body |
| 20 | 17 B | Person detection: `[detected, x, y, w, h]`, with x/y/w/h as float32 fractions of the frame. Stays live while tracking is off. |

Other details:

- **Camera in use:** read from CoreMediaIO (`kCMIODevicePropertyDeviceIsRunningSomewhere`). This doesn't need camera permission.
- **Why tracking is our own:** firmware tracking pulls any manual tilt back within about a second. So the height offset is a small follow loop instead. Every 0.5 s it reads the person box from selector 20 and moves only if you've left the calm zone. After each move it waits 1.5 s.
- **Security:** the server listens on `127.0.0.1` only. POST requests need a matching `Origin` header, which blocks other websites and DNS rebinding.

The extension unit selector map builds on [vrwallace/Insta360-Link-1-and-2-Controller-for-Linux](https://github.com/vrwallace/Insta360-Link-1-and-2-Controller-for-Linux). The mode-layer behaviour and selector 20 were found while building this tool.

## License

[MIT](LICENSE)

---

**Not affiliated with Insta360.** This is a private hobby project. It is not affiliated with, endorsed by, or connected to Arashi Vision Inc. (Insta360) in any way. "Insta360" and "Insta360 Link" are trademarks of Arashi Vision Inc. and are used here only to name the camera this tool works with. All other trademarks belong to their respective owners. Use at your own risk.
