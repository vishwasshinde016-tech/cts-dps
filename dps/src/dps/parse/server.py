"""Parser container service (Phase 2): the worker sends file bytes over a Unix socket, gets a page model back.

Run inside the parser container (``network_mode: none``, non-root, read-only root, all capabilities dropped):
``python -m dps.parse.server /run/dps-parser/parser.sock``. Only the file and a small request header go in;
only the page model JSON or an error code comes out. Each request is parsed in a fresh sandboxed child.

Frame: 4-byte big-endian header length, JSON header, 8-byte big-endian body length, body bytes.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import struct
import sys

from dps.ingest.inspect import InspectionFailed
from dps.parse import sandbox

log = logging.getLogger(__name__)
MAX_BODY = 110 * 1024 * 1024


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    parts, got = [], 0
    while got < n:
        chunk = sock.recv(min(1 << 20, n - got))
        if not chunk:
            raise ConnectionError("peer closed")
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def send_frame(sock: socket.socket, header: dict, body: bytes = b"") -> None:
    raw = json.dumps(header).encode("utf-8")
    sock.sendall(struct.pack(">I", len(raw)) + raw + struct.pack(">Q", len(body)) + body)


def recv_frame(sock: socket.socket) -> tuple[dict, bytes]:
    (hlen,) = struct.unpack(">I", _recv_exact(sock, 4))
    if hlen > 1 << 20:
        raise ValueError("header too large")
    header = json.loads(_recv_exact(sock, hlen))
    (blen,) = struct.unpack(">Q", _recv_exact(sock, 8))
    if blen > MAX_BODY:
        raise ValueError("body too large")
    return header, _recv_exact(sock, blen)


def handle(header: dict, body: bytes, pool: str) -> dict:
    req = sandbox.ParseRequest(body, header["format"], header["sha256"], header.get("versionId"),
                               bool(header.get("ocr", True)), pool == "HEAVY")
    try:
        model = sandbox.run(req, sandbox.Limits.for_pool(pool), header.get("pagesHint"),
                            target=header.get("target", "parse") if os.environ.get("DPS_PARSER_TEST") else "parse")
    except InspectionFailed as ex:
        return {"error": ex.code, "message": str(ex)}
    except sandbox.SandboxError as ex:
        return {"error": ex.code, "message": str(ex)}
    return {"ok": True, "model": model}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # pragma: no cover - exercised by the socket test on Linux
        try:
            header, body = recv_frame(self.request)
        except (ValueError, ConnectionError, json.JSONDecodeError):
            return
        reply = handle(header, body, self.server.pool)  # type: ignore[attr-defined]
        payload = json.dumps(reply).encode("utf-8")
        send_frame(self.request, {"len": len(payload)}, payload)


class SocketParser:
    """The worker's side: one connection per parse request."""

    def __init__(self, path: str, timeout: float = 960.0) -> None:
        self.path, self.timeout = path, timeout

    def parse(self, req: sandbox.ParseRequest, pages_hint: int | None = None, target: str = "parse") -> dict:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
            send_frame(sock, {"format": req.fmt, "sha256": req.sha256, "versionId": req.version_id,
                              "ocr": req.ocr, "pagesHint": pages_hint, "target": target}, req.data)
            _, payload = recv_frame(sock)
        except (OSError, ConnectionError) as ex:
            raise sandbox.ParserCrash(f"Parser service unreachable: {type(ex).__name__}") from ex
        finally:
            sock.close()
        reply = json.loads(payload)
        if reply.get("ok"):
            return reply["model"]
        code, message = reply.get("error", "PARSER_CRASH"), reply.get("message", "")
        errors = {"PARSE_TIMEOUT": sandbox.ParseTimeout, "PARSER_CRASH": sandbox.ParserCrash,
                  "OUT_OF_MEMORY": sandbox.ParserOutOfMemory}
        if code in errors:
            raise errors[code](message)
        raise InspectionFailed(code, message)


def serve(path: str, pool: str = "STANDARD") -> None:  # pragma: no cover - long-running service
    if os.path.exists(path):
        os.unlink(path)
    server = socketserver.ThreadingUnixStreamServer(path, _Handler)  # type: ignore[attr-defined]
    server.pool = pool  # type: ignore[attr-defined]
    os.chmod(path, 0o600)  # parser and worker run as the same uid 10001
    log.info("dps parser listening on %s (%s pool)", path, pool)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    serve(sys.argv[1] if len(sys.argv) > 1 else "/run/dps-parser/parser.sock",
          os.environ.get("DPS_WORKER_POOL", "STANDARD"))
