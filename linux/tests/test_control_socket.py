"""Tests for the local control socket (blitztext trigger / stop / cancel / status).

The socket is the Wayland escape hatch: pynput's X11 RECORD backend delivers no
key events under a Wayland compositor, so the desktop's own shortcut manager is
wired to `blitztext trigger` instead.
"""

import json
import os
import stat
import threading

import pytest

from blitztext.config import Config, Workflow
from blitztext.control import ControlServer, send, socket_path, ControlError


class StubDaemon:
    """Records what the control server asks it to do, without any audio."""

    def __init__(self, workflows=None):
        self.cfg = Config(workflows=workflows if workflows is not None else [
            Workflow(name="Transcribe", hotkey="<ctrl>+<alt>+d", mode="transcribe"),
            Workflow(name="Nicer email", hotkey="<ctrl>+<alt>+e", mode="rewrite"),
        ])
        self._route_workflow = Workflow(name="Voice", hotkey="<ctrl>+<alt>+<space>", mode="route")
        self.calls = []
        self.ready = True
        self._busy = False
        self._recording = None
        self._streaming = None
        self._active_workflow = None

    def toggle(self, wf):
        self.calls.append(("toggle", wf.name))

    def finish_dictation(self, send_enter=False):
        self.calls.append(("finish", send_enter))

    def cancel_dictation(self):
        self.calls.append(("cancel",))

    @property
    def is_recording(self):
        return self._recording is not None

    @property
    def is_busy(self):
        return self._busy

    def active_preset_name(self):
        return self._active_workflow.name if self._active_workflow else None


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A live control server on a private socket path, torn down afterwards."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    srv = ControlServer(StubDaemon())
    srv.start()
    yield srv
    srv.stop()


# -- socket path -------------------------------------------------------------
def test_socket_path_uses_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socket_path() == tmp_path / "blitztext" / "control.sock"


def test_socket_path_falls_back_to_config_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert socket_path().name == "control.sock"
    assert ".config" in str(socket_path())


# -- permissions -------------------------------------------------------------
def test_socket_is_owner_only(server):
    """Other users on the machine must not be able to start dictation."""
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700


def test_socket_removed_on_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    srv = ControlServer(StubDaemon())
    srv.start()
    assert srv.path.exists()
    srv.stop()
    assert not srv.path.exists()


# -- dispatch ----------------------------------------------------------------
def test_ping(server):
    assert send({"cmd": "ping"}) == {"ok": True, "pong": True}


def test_list_returns_preset_names(server):
    res = send({"cmd": "list"})
    assert res["ok"] is True
    assert res["presets"] == ["Transcribe", "Nicer email"]
    assert res["default"] == "Voice"


def test_status_reports_state(server):
    assert send({"cmd": "status"})["state"] == "ready"
    server._d._busy = True
    assert send({"cmd": "status"})["state"] == "busy"
    server._d._busy = False
    server._d._recording = object()
    assert send({"cmd": "status"})["state"] == "recording"


def test_trigger_by_name_is_case_insensitive(server):
    res = send({"cmd": "trigger", "preset": "nicer EMAIL"})
    assert res == {"ok": True, "triggered": "Nicer email"}
    assert server._d.calls == [("toggle", "Nicer email")]


def test_trigger_by_one_based_index(server):
    send({"cmd": "trigger", "preset": "1"})
    assert server._d.calls == [("toggle", "Transcribe")]


def test_trigger_without_preset_uses_voice_routing(server):
    assert send({"cmd": "trigger"}) == {"ok": True, "triggered": "Voice"}
    assert server._d.calls == [("toggle", "Voice")]


def test_trigger_by_voice_alias(server):
    send({"cmd": "trigger", "preset": "voice"})
    assert server._d.calls == [("toggle", "Voice")]


def test_trigger_unknown_preset_lists_alternatives(server):
    res = send({"cmd": "trigger", "preset": "нет такого"})
    assert res["ok"] is False
    assert "Transcribe" in res["error"]
    assert server._d.calls == []          # nothing was started


def test_trigger_index_out_of_range(server):
    res = send({"cmd": "trigger", "preset": "99"})
    assert res["ok"] is False
    assert "1-2" in res["error"]
    assert server._d.calls == []


def test_stop_forwards_enter_flag(server):
    send({"cmd": "stop", "enter": True})
    send({"cmd": "stop"})
    assert server._d.calls == [("finish", True), ("finish", False)]


def test_cancel(server):
    assert send({"cmd": "cancel"}) == {"ok": True, "cancelled": True}
    assert server._d.calls == [("cancel",)]


# -- malformed input ---------------------------------------------------------
def test_unknown_command_is_rejected(server):
    res = send({"cmd": "nope"})
    assert res["ok"] is False
    assert "unknown command" in res["error"]


def test_malformed_json_is_rejected(server):
    res = send_raw(server, "not json\n")
    assert res["ok"] is False
    assert "bad request" in res["error"]


def test_non_object_json_is_rejected(server):
    res = send_raw(server, "[1,2,3]\n")
    assert res["ok"] is False
    assert "bad request" in res["error"]


def test_oversized_request_is_refused(server):
    res = send_raw(server, '{"cmd":"ping","pad":"' + "x" * 20000 + '"}\n')
    assert res["ok"] is False
    assert server._d.calls == []


def send_raw(server, raw: str) -> dict:
    """Talk to the socket directly, bypassing the client's JSON encoding."""
    import socket as _socket

    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(str(server.path))
        s.sendall(raw.encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode())


# -- single instance ---------------------------------------------------------
def test_second_server_refuses_to_steal_a_live_socket(server):
    second = ControlServer(StubDaemon())
    second.start()                       # must not raise
    assert second._thread is None        # and must not have started serving
    second.stop()                        # must not remove the live socket
    assert server.path.exists()


def test_stale_socket_file_is_reclaimed(tmp_path, monkeypatch):
    """A socket left behind by a crashed daemon is replaced, not treated as live."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    srv = ControlServer(StubDaemon())
    srv.start()
    path = srv.path
    srv.stop()                           # stop() unlinks, so recreate the leftover
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    fresh = ControlServer(StubDaemon())
    fresh.start()
    assert fresh._thread is not None
    assert send({"cmd": "ping"})["pong"] is True
    fresh.stop()


# -- client without a daemon -------------------------------------------------
def test_client_reports_missing_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(ControlError, match="no control socket"):
        send({"cmd": "ping"})


def test_peer_uid_is_readable_locally(tmp_path):
    """SO_PEERCRED must report our own uid, which is what the access check relies on."""
    from blitztext.control import _peer_uid
    import socket as _socket

    # Own socket rather than the server's: ControlServer._serve is already
    # calling accept() on that one, so a second accepter would just race it.
    path = tmp_path / "probe.sock"
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    got = {}
    ready = threading.Event()

    def accept_one():
        conn, _ = srv.accept()
        try:
            got["uid"] = _peer_uid(conn)
        finally:
            conn.close()
            ready.set()

    t = threading.Thread(target=accept_one, daemon=True)
    t.start()
    client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    client.connect(str(path))
    client.close()
    try:
        assert ready.wait(5) is True
        t.join(2)
        assert got["uid"] == os.getuid()
    finally:
        srv.close()
