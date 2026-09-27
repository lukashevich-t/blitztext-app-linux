"""Local control socket — lets an external program drive a running daemon.

Why this exists: on a Wayland session the X11 ``RECORD`` extension that
``pynput`` relies on delivers no key events at all, so the global hotkeys are
silently dead. The desktop's own shortcut manager (GNOME's *Settings →
Keyboard → Custom Shortcuts*, KDE's *Shortcut Editor*, sxhkd, …) *does* work on
Wayland, because the compositor handles the key. Wiring those to a command that
talks to the already-running daemon sidesteps the whole problem — so the daemon
exposes a small socket that a one-line command can hit.

Security: the socket lives in ``$XDG_RUNTIME_DIR`` (mode 0700, per-user,
removed at logout) and is created mode 0600. Every accepted connection is
additionally checked with ``SO_PEERCRED`` and refused unless it comes from our
own uid, so no other user on the machine can start dictation.

Protocol: one JSON object per line, newline-terminated, over ``SOCK_STREAM``.
The reply is a single JSON line, so it is easy to poke by hand::

    $ socat - UNIX-CONNECT:"$XDG_RUNTIME_DIR/blitztext/control.sock"
    {"cmd": "status"}
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from pathlib import Path

from .logbuffer import log

# A control request is a single short command; anything bigger is abuse.
_MAX_REQUEST = 8192
_ACCEPT_TIMEOUT = 1.0


def socket_path() -> Path:
    """Where the control socket lives.

    ``$XDG_RUNTIME_DIR`` is the right home for a runtime socket: it is per-user,
    mode 0700 and wiped on logout, so no stale file can outlive the session. The
    config dir is only a fallback for the rare case where it is unset.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "blitztext" / "control.sock"
    return Path.home() / ".config" / "blitztext" / "control.sock"


def _peer_uid(conn: socket.socket) -> int | None:
    """uid of the process on the other end, or None if the kernel won't say."""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except (OSError, AttributeError):
        return None


class ControlServer:
    """Accepts control commands on a unix socket and calls back into the daemon.

    The callbacks run on the accept thread, exactly like the pynput hotkey
    callbacks do — ``Daemon`` already marshals its GTK work through
    ``GLib.idle_add``, so it is safe to call ``toggle``/``finish`` from here.
    """

    def __init__(self, daemon):
        self._d = daemon
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._owns_path = False
        self.path = socket_path()

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self._bind()
        except OSError as exc:
            # Never fatal: a missing control socket only costs the CLI trigger,
            # the tray menu and the hotkeys keep working.
            log(f"Control socket unavailable at {self.path}: {exc}", level="WARNING")
            return
        self._thread = threading.Thread(target=self._serve, name="blitztext-control",
                                        daemon=True)
        self._thread.start()
        log(f"Control socket: {self.path}  (blitztext trigger <preset>)")

    def _bind(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass  # a pre-existing dir we don't own; the peer check still applies
        if self.path.exists():
            # A socket left behind by a crashed daemon. Probe it: if someone
            # answers, the daemon is alive and we must not steal the path.
            if _probe(self.path):
                raise OSError("another Blitztext instance is already listening")
            self.path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        sock.listen(8)
        sock.settimeout(_ACCEPT_TIMEOUT)
        self._sock = sock
        self._owns_path = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        # Only unlink a path we actually bound. A server that failed to start
        # (because a live instance owns the path) must not delete the running
        # instance's socket on its way out.
        if self._owns_path:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._owns_path = False

    # -- serve ----------------------------------------------------------------
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()          # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    self._handle(conn)
                except Exception as exc:                # noqa: BLE001
                    log(f"Control socket error: {exc}", level="WARNING")

    def _handle(self, conn: socket.socket) -> None:
        uid = _peer_uid(conn)
        if uid is not None and uid != os.getuid():
            log(f"Control socket: refused connection from uid {uid}", level="WARNING")
            _reply(conn, {"ok": False, "error": "not permitted"})
            return
        conn.settimeout(5.0)
        chunks, total = [], 0
        while total <= _MAX_REQUEST:
            try:
                buf = conn.recv(4096)
            except (socket.timeout, OSError):
                break
            if not buf:
                break
            chunks.append(buf)
            total += len(buf)
            if b"\n" in buf:
                break
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        if not raw:
            _reply(conn, {"ok": False, "error": "empty request"})
            return
        try:
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            _reply(conn, {"ok": False, "error": f"bad request: {exc}"})
            return
        _reply(conn, self._dispatch(req))

    # -- commands -------------------------------------------------------------
    def _dispatch(self, req: dict) -> dict:
        cmd = str(req.get("cmd", "")).strip().lower()
        d = self._d
        if cmd == "ping":
            return {"ok": True, "pong": True}
        if cmd == "list":
            return {"ok": True,
                    "presets": [w.name for w in d.cfg.workflows],
                    "default": d._route_workflow.name}
        if cmd == "status":
            state = ("recording" if d.is_recording else
                     "busy" if d.is_busy else
                     "ready" if d.ready else "loading")
            return {"ok": True, "state": state, "busy": d.is_busy,
                    "preset": d.active_preset_name()}
        if cmd == "trigger":
            wf, err = _resolve(d, req.get("preset"))
            if err:
                return {"ok": False, "error": err}
            d.toggle(wf)
            return {"ok": True, "triggered": wf.name}
        if cmd == "stop":
            d.finish_dictation(send_enter=bool(req.get("enter", False)))
            return {"ok": True, "stopped": True}
        if cmd == "cancel":
            d.cancel_dictation()
            return {"ok": True, "cancelled": True}
        return {"ok": False,
                "error": f"unknown command {cmd!r}; try: ping, list, status, trigger, stop, cancel"}


def _resolve(daemon, name) -> tuple:
    """Map a preset name or 1-based index to a workflow; the default if omitted."""
    if name is None or (isinstance(name, str) and not name.strip()):
        return daemon._route_workflow, None
    workflows = daemon.cfg.workflows
    if isinstance(name, int) or (isinstance(name, str) and name.strip().isdigit()):
        idx = int(name) - 1
        if not 0 <= idx < len(workflows):
            return None, f"no preset #{name}; 1-{len(workflows)}"
        return workflows[idx], None
    wanted = str(name).strip().lower()
    for wf in workflows:
        if wf.name.strip().lower() == wanted:
            return wf, None
    # Also accept the synthetic voice-routing workflow by name.
    if wanted in (daemon._route_workflow.name.lower(), "voice", "default"):
        return daemon._route_workflow, None
    names = ", ".join(w.name for w in workflows) or "(none)"
    return None, f"no preset named {name!r}; available: {names}"


def _reply(conn: socket.socket, payload: dict) -> None:
    try:
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def _probe(path: Path, timeout: float = 0.5) -> bool:
    """True if something is accepting on `path` right now."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


# -- client ------------------------------------------------------------------
class ControlError(RuntimeError):
    pass


def send(request: dict, *, timeout: float = 5.0) -> dict:
    """Send one command to the running daemon and return its reply."""
    path = socket_path()
    if not path.exists():
        raise ControlError(
            f"no control socket at {path} — is Blitztext running? Start it with "
            f"'blitztext tray' or 'blitztext run'.")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        s.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        raise ControlError(f"cannot talk to Blitztext at {path}: {exc}") from exc
    finally:
        s.close()
    if not buf.strip():
        raise ControlError("Blitztext closed the connection without replying")
    return json.loads(buf.decode("utf-8", "replace"))
