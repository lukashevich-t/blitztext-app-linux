# Wayland Hotkeys — The Control Socket

Blitztext's global hotkeys are captured by [`pynput`](https://pynput.readthedocs.io/),
which on Linux uses the X11 **RECORD** extension. **On a Wayland session that
extension delivers nothing**, so every configured hotkey is silently dead while
the app still reports "Ready". The tray menu, wakeword and the panel keep working.

The fix has two halves:

| | Problem | Solution |
|---|---|---|
| **Capturing the key** | The compositor owns the keyboard; X11 `RECORD` sees nothing. | Bind a shortcut in the **desktop's own** shortcut manager (GNOME, KDE, sxhkd) to a command that talks to the running daemon. |
| **Typing the text back** | Something must synthesize keystrokes, which Wayland also forbids to X clients. | `ydotool` (uinput) or `wtype` (wlroots compositors only). |

Blitztext exposes a small **local control socket** for the first half, so any
shortcut manager can start dictation with a one-line command.

---

## 1. How it works

While a Blitztext instance is running (`blitztext tray`, `blitztext gui` or
`blitztext run`) it listens on a unix socket:

```
$XDG_RUNTIME_DIR/blitztext/control.sock     # usually /run/user/1000/blitztext/control.sock
```

The directory is mode `0700` and the socket `0600`, and every connection is
checked with `SO_PEERCRED`, so no other user on the machine can start dictation
in your session. The socket is removed on shutdown and, being under
`$XDG_RUNTIME_DIR`, at logout.

`blitztext trigger` is a thin client for it. Bind *that* to a desktop shortcut
and the compositor does the key handling — which is exactly what X11 `RECORD`
could not do.

---

## 2. Commands

```
blitztext trigger [<preset>]    start dictation; with no preset uses voice routing
blitztext trigger --list        list the available presets
blitztext stop [--enter]        stop recording and paste the transcript
blitztext cancel                discard the current recording
blitztext status                what the running instance is doing
```

`<preset>` accepts a name (case-insensitive) or a 1-based number. Pressing the
same combination again stops and pastes, so one shortcut per preset is enough —
`trigger` is a toggle, exactly like the hotkey it replaces.

```bash
$ blitztext trigger --list
Presets (use the name or the number):
   1  Transcribe
   2  Nicer email
   3  Improve text
   -  Voice   (voice routing; used when no preset is given)

$ blitztext trigger "Nicer email"     # start that preset
$ blitztext status
blitztext: recording (Nicer email)
$ blitztext stop --enter              # stop, paste, press Enter
```

Exit code is `0` on success, `1` if the daemon is not running or the preset name
is unknown, which makes the commands safe to use from scripts.

---

## 3. Set up text delivery (do this first)

Without an input injector the app will transcribe your speech and then have
nowhere to put it. Check what you have:

```bash
command -v ydotool wtype wl-copy
```

### GNOME / any compositor: `ydotool`

`wtype` speaks a wlr text-input protocol that **GNOME/mutter does not
implement**, so on GNOME `ydotool` is the only option. It needs its daemon
running and write access to `/dev/uinput`:

```bash
# 1. allow uinput without sudo (standard ydotool rule)
echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-ydotool.rules
sudo usermod -aG input "$USER"      # log out and back in for this to apply

# 2. start the daemon (auto-start it, see §6)
sudo ydotoold
```

Verify — this should type into the window you focus:

```bash
ydotool type "delivery works"
```

If it prints `failed to connect socket '/run/user/.../.ydotool_socket'`, the
daemon is not running. If it prints a permission error, the `input` group change
has not taken effect yet.

### Clipboard (strongly recommended)

For long or multi-line text Blitztext switches to a clipboard paste, which is
instant instead of typing character by character. That needs a clipboard tool:

```bash
sudo apt install wl-clipboard     # Wayland; on X11: xclip or xsel
```

---

## 4. Bind a shortcut — GNOME

**Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts →
+**. Name it, set the combination, and use this command:

```
blitztext trigger Transcribe
```

Do that once per preset you want. Preset names must match `blitztext trigger
--list` exactly.

Doing the same from a terminal (handy for several at once) — note that
`gsettings set` needs a **list** value, hence the quotes:

```bash
SCHEMA=org.gnome.settings-daemon.plugins.media-keys
add() {  # $1 = combo, $2 = preset name
  gsettings set "$SCHEMA" custom-keybindings "['$1', 'blitztext trigger $2']"
}
add '<Control><Alt>d' 'Transcribe'
add '<Control><Alt>e' 'Nicer email'
add '<Control><Alt>space' ''          # no preset = voice routing
gsettings get "$SCHEMA" custom-keybindings
```

`add '<Control><Alt>space' ''` binds the bare space combo, which voice routing
uses to pick the preset from what you say next.

## 5. Bind a shortcut — KDE, sxhkd, anything else

Any tool that can run a command on a key works:

```bash
# sxhkd
"blitztext trigger Transcribe"
  super + alt + d
```

```bash
# KDE: System Settings → Shortcuts → Custom Shortcuts → Add…
# Command: blitztext trigger Transcribe
```

For scripts, watch the daemon state instead of binding separate stop keys:

```bash
blitztext status | grep -q recording || blitztext trigger Transcribe
```

---

## 6. Autostart

Blitztext must be running *before* the shortcut is pressed. The tray icon
normally handles this (your desktop's "Start on login" / Startup Applications
entry), or use the bundled systemd user unit:

```bash
systemctl --user enable --now blitztext.service
```

Add `ydotoold` next to it if you went the `ydotool` route — a root service is
required for `/dev/uinput`:

```bash
sudo tee /etc/systemd/system/ydotoold.service <<'EOF'
[Unit]
Description=ydotool uinput daemon
[Service]
ExecStart=/usr/bin/ydotoold
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now ydotoold.service
```

---

## 7. Troubleshooting

**`blitztext status` says "no control socket"** — no instance is running. Start
one with `blitztext tray` (or `run`). The message is printed by the client, not
the daemon, so you can run the commands at any time.

**The command works from a terminal but not from the desktop shortcut** — the
compositor spawns the command with a minimal environment. If `XDG_RUNTIME_DIR`
is missing there, the client falls back to `~/.config/blitztext/control.sock`
and finds nothing. Use an absolute path in the shortcut command:

```
/usr/bin/blitztext trigger Transcribe
```

**Text is transcribed but never appears** — delivery is broken, not capture.
Check §3: `ydotool type "test"` must work before anything else. Blitztext
swallows injector failures, so this looks like a lost recording rather than an
error. Watch **Settings → Log** for the stage that ran.

**`Ctrl+Alt+E` does the wrong thing** — two presets share a hotkey, and only the
last one is registered. This is reported at startup as
`Hotkey … is bound twice`. Give one of them a different combination in
**Settings → Workflows**.

**Recording starts but stops immediately** — the silence auto-stop fires after
~2 s of no speech. Raise `input.silence_seconds` or check the microphone level
meter in **Settings → General**.

---

## 8. Manual use and debugging

The socket speaks one JSON object per line, so you can drive it by hand:

```bash
SOCK="$XDG_RUNTIME_DIR/blitztext/control.sock"

printf '{"cmd":"status"}\n'  | socat - UNIX-CONNECT:"$SOCK"
printf '{"cmd":"list"}\n'    | socat - UNIX-CONNECT:"$SOCK"
printf '{"cmd":"trigger","preset":"Transcribe"}\n' | socat - UNIX-CONNECT:"$SOCK"
```

```json
{"ok": true, "state": "ready", "busy": false, "preset": null}
```

Commands: `ping`, `list`, `status`, `trigger` (optional `preset`), `stop`
(optional `enter`), `cancel`. Every reply is a JSON object with `ok`, plus
`error` when something is refused.

---

## See also

- [docs/setup.md](setup.md) — engine and audio troubleshooting
- [MANUAL.md](../MANUAL.md) — all settings, including `input.mode`
- [docs/privacy.md](privacy.md) — what leaves the machine
