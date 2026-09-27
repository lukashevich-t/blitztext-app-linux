"""Blitztext application entry point (CLI + GUI/tray launcher).

The thin ``__main__.py`` shim imports ``main()`` from here, so the app's code
lives in ``blitztext.py`` while ``python -m blitztext`` still works.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, ensure_default, load

# Held for the process lifetime to enforce a single running instance.
_SINGLE_INSTANCE_SOCK = None


def _acquire_single_instance() -> bool:
    """Bind an abstract unix socket; False if another instance already holds it."""
    global _SINGLE_INSTANCE_SOCK
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind("\0blitztext-single-instance")  # abstract namespace (auto-freed on exit)
    except OSError:
        return False
    _SINGLE_INSTANCE_SOCK = sock
    return True


def _control_cmd(args) -> int:
    """Drive the running daemon over its local control socket."""
    from .control import ControlError, send

    if args.cmd == "trigger" and args.list:
        req = {"cmd": "list"}
    elif args.cmd == "trigger":
        req = {"cmd": "trigger", "preset": args.preset}
    elif args.cmd == "stop":
        req = {"cmd": "stop", "enter": bool(args.enter)}
    elif args.cmd == "cancel":
        req = {"cmd": "cancel"}
    else:
        req = {"cmd": "status"}

    try:
        res = send(req)
    except ControlError as exc:
        print(f"blitztext: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "trigger" and args.list:
        if not res.get("ok"):
            print(f"blitztext: {res.get('error')}", file=sys.stderr)
            return 1
        print("Presets (use the name or the number):")
        for i, name in enumerate(res.get("presets", []), 1):
            print(f"  {i:>2}  {name}")
        print(f"   -  {res.get('default')}   (voice routing; used when no preset is given)")
        return 0

    if not res.get("ok"):
        print(f"blitztext: {res.get('error')}", file=sys.stderr)
        return 1
    if args.cmd == "status":
        print(f"blitztext: {res.get('state')}"
              + (f" ({res['preset']})" if res.get("preset") else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blitztext", description="Native dictation for Linux.")
    parser.add_argument("--version", action="version", version=f"blitztext {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("tray", help="Run in the system tray with a workflow menu (default).")
    sub.add_parser("gui", help="Open the control panel window.")
    sub.add_parser("run", help="Start the headless hotkey daemon (no window/tray).")
    sub.add_parser("config-path", help="Print the config file path and exit.")
    p_tx = sub.add_parser("transcribe", help="Transcribe a WAV file and print the text (no hotkeys).")
    p_tx.add_argument("audio", type=Path)

    p_tr = sub.add_parser("trigger", help="Start/stop dictation in the running instance. "
                                          "Works on Wayland, where the global hotkeys do not.")
    p_tr.add_argument("preset", nargs="?", default=None,
                      help="preset name or 1-based index; omit for the voice-routing preset")
    p_tr.add_argument("--list", action="store_true", help="list the available presets and exit")
    p_st = sub.add_parser("stop", help="Stop recording and paste the transcript.")
    p_st.add_argument("--enter", action="store_true", help="also press Enter after pasting")
    sub.add_parser("cancel", help="Discard the current recording.")
    sub.add_parser("status", help="Show what the running instance is doing.")

    args = parser.parse_args(argv)
    cmd = args.cmd or "tray"

    from .logbuffer import install_logging
    install_logging()

    if cmd == "config-path":
        print(ensure_default(CONFIG_PATH))
        return 0

    if cmd in ("trigger", "stop", "cancel", "status"):
        return _control_cmd(args)

    # Only one live daemon/tray/gui at a time — prevents duplicate wakeword
    # listeners and recorders (which caused notification storms).
    if cmd in ("gui", "tray", "run") and not _acquire_single_instance():
        print("[blitztext] already running — not starting a second instance.", file=sys.stderr)
        return 0

    if cmd in ("gui", "tray"):
        ensure_default(CONFIG_PATH)
        from .gtkui import run_gui

        return run_gui(tray_mode=(cmd == "tray"))

    cfg = load()

    if cmd == "transcribe":
        from .transcribe import Transcriber

        tx = Transcriber(cfg.model, cfg.device, cfg.compute_type, cfg.beam_size)
        print(tx.transcribe(args.audio, language=cfg.language))
        return 0

    # cmd == "run"
    from .daemon import Daemon

    try:
        Daemon(cfg).run()
    except KeyboardInterrupt:
        print("\n[blitztext] stopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
