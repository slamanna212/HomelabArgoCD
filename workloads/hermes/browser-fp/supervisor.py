#!/usr/bin/env python3
"""Install a fingerprint shim into every page target of a CDP browser at document_start.

Runs beside the browser as a sidecar process. Stdlib only (no venv, no third-party
packages) so it works in any Linux agent image whose base has python3.

Mechanism: browser-level Target.setAutoAttach with waitForDebuggerOnStart=True and
flatten=True. Every new page target pauses before its first script runs; we push the
shim with Page.addScriptToEvaluateOnNewDocument + Runtime.evaluate, then release the
target with Runtime.runIfWaitingForDebugger. Page scripts therefore never observe an
unpatched navigator/WebGL/document state. Existing pages are patched on attach too.

Env:
  FP_JS     shim source path              (default /browser-fp/fp.js)
  CDP_HTTP  CDP discovery base            (default http://127.0.0.1:9222)
  FP_LOG    log file, "-" for stdout      (default -)
"""
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

FP_PATH = os.environ.get("FP_JS", "/browser-fp/fp.js")
CDP_HTTP = os.environ.get("CDP_HTTP", "http://127.0.0.1:9222").rstrip("/")
LOG_PATH = os.environ.get("FP_LOG", "-")


def log(msg):
    line = "browser-fp: %s" % msg
    if LOG_PATH == "-":
        print(line, flush=True)
    else:
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line))
        except Exception:
            pass


class WS:
    """Minimal RFC6455 client: text/continuation frames, ping/pong, no extensions."""

    def __init__(self, url, timeout=20):
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("handshake closed")
            buf += chunk
            if len(buf) > 65536:
                raise IOError("handshake too large")
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise IOError("handshake refused: %r" % status[:120])

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, payload, opcode):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, obj):
        self._frame(json.dumps(obj).encode(), 0x1)

    def recv(self):
        parts = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, ln = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(ln) if ln else b""
            if opcode == 0x9:
                self._frame(payload, 0xA)
                continue
            if opcode == 0x8:
                raise IOError("closed by peer")
            if opcode in (0x0, 0x1, 0x2):
                parts += payload
                if fin:
                    return json.loads(parts.decode("utf-8", "replace"))

    def close(self):
        try:
            self._frame(b"", 0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def browser_ws():
    with urllib.request.urlopen(CDP_HTTP + "/json/version", timeout=5) as fh:
        return json.load(fh)["webSocketDebuggerUrl"]


class Injector:
    def __init__(self, source):
        self.source = source
        self.seq = 0
        self.sessions = set()
        self.ws = None

    def _send(self, method, params=None, session=None):
        self.seq += 1
        msg = {"id": self.seq, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(msg)
        return self.seq

    def patch_session(self, session):
        """Install the shim, then release the target. Never leaves a target paused."""
        try:
            # Registered before the document starts: this is the document_start path.
            self._send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": self.source, "runImmediately": True},
                session,
            )
            # A target created with a URL already in flight has committed its first
            # document before we attach, so also patch the live document. The shim
            # is idempotent (guarded) so running it twice is harmless.
            self._send("Page.enable", {}, session)
            # A real browser's tab is the frontmost one; a backgrounded tab reports
            # hidden/unfocused (and 0x0 outer geometry), which WAFs read as headless.
            try:
                self._send("Page.bringToFront", {}, session)
            except Exception:
                pass
            self._send(
                "Runtime.evaluate",
                {"expression": self.source, "returnByValue": False, "awaitPromise": False},
                session,
            )
        except Exception as exc:
            log("inject failed on %s: %s" % (session, exc))
        finally:
            try:
                self._send("Runtime.runIfWaitingForDebugger", {}, session)
            except Exception:
                pass

    def repatch(self, session):
        """Re-assert on a live document (cheap, idempotent): covers a navigation that
        started before addScriptToEvaluateOnNewDocument was registered."""
        try:
            self._send(
                "Runtime.evaluate",
                {"expression": self.source, "returnByValue": False, "awaitPromise": False},
                session,
            )
        except Exception:
            pass

    def handle(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget":
            session = params.get("sessionId")
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and session and session not in self.sessions:
                self.sessions.add(session)
                self.patch_session(session)
        elif method == "Target.detachedFromTarget":
            self.sessions.discard(params.get("sessionId"))
        elif method in ("Page.frameNavigated", "Page.loadEventFired"):
            session = msg.get("sessionId")
            if session in self.sessions:
                self.repatch(session)
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and not info.get("attached"):
                try:
                    self._send("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
                except Exception:
                    pass
        elif method is None and "id" in msg and msg.get("error"):
            log("cdp error on id %s: %s" % (msg["id"], msg["error"]))

    def run_forever(self):
        backoff = 1.0
        while True:
            try:
                self.ws = WS(browser_ws())
                self.sessions.clear()
                self._send("Target.setDiscoverTargets", {"discover": True})
                self._send(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                )
                backoff = 1.0
                log("attached to browser")
                while True:
                    self.handle(self.ws.recv())
            except Exception as exc:
                log("reconnect in %.0fs: %s" % (backoff, exc))
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def main():
    try:
        with open(FP_PATH) as fh:
            source = fh.read()
    except OSError as exc:
        log("cannot read %s: %s" % (FP_PATH, exc))
        return 1
    if not source.strip():
        log("%s is empty; nothing to inject" % FP_PATH)
        return 1
    log("shim %d bytes from %s" % (len(source), FP_PATH))
    Injector(source).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
