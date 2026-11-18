"""The daily app log: every container of the stack, one readable file per day (docs/observability.md).

    logs/covenexa-2026-11-09.log
    2026-11-09 10:42:01.120Z ui               10.0.0.5 "POST /api/v1/documents" 201 cid=3f2a...
    2026-11-09 10:42:01.950Z core             ... INFO [3f2a... rm-anil] [CP-1/-] ... upload forwarded to DPS
    2026-11-09 10:42:08.400Z dps-worker       ... [job 77e0 doc d81c] dps.events: FOUND DSCR >= 1.25 (page 42) ...

It reads the containers' output through the Docker Engine API (the socket is mounted read-only into its container)
and appends each line, with the time and the service, to the file of that day (UTC). Lines are masked for secrets
again on the way in. Files older than ``LOG_KEEP_DAYS`` (30) are deleted. After a restart it carries on from the
last line it wrote, so no line is written twice. Standard library only.

    python -m dps.logcollector
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dps.observability import redact

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
LOG_DIR = Path(os.environ.get("LOG_DIR", "/logs"))
KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "30"))
PROJECT = os.environ.get("LOG_PROJECT", "covenant-platform")
SELF = os.environ.get("LOG_SELF_SERVICE", "log-collector")
PREFIX = "covenexa-"


def demux(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Docker's log stream without a TTY: frames of an 8-byte header (stream, 0, 0, 0, size) and the payload.
    Returns the complete payloads and the bytes of an incomplete frame left over."""
    out = []
    while len(buffer) >= 8:
        size = struct.unpack(">I", buffer[4:8])[0]
        if len(buffer) < 8 + size:
            break
        out.append(buffer[8:8 + size])
        buffer = buffer[8 + size:]
    return out, buffer


def parse_line(raw: str) -> tuple[datetime | None, str]:
    """``2026-11-09T10:42:01.123456789Z text`` (Docker's timestamp) into the time and the text."""
    stamp, _, text = raw.partition(" ")
    try:
        when = datetime.strptime(stamp[:26].rstrip("Z") + "Z", "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None, raw
    return when, text


def format_line(when: datetime, service: str, text: str) -> str:
    return f"{when:%Y-%m-%d %H:%M:%S}.{when.microsecond // 1000:03d}Z {service:<16} {redact(text.rstrip())}\n"


def file_for(day: date, folder: Path = LOG_DIR) -> Path:
    return folder / f"{PREFIX}{day:%Y-%m-%d}.log"


def prune(folder: Path, keep_days: int, today: date) -> list[Path]:
    """Deletes daily files older than ``keep_days``; returns what it deleted."""
    gone = []
    for f in folder.glob(f"{PREFIX}*.log"):
        try:
            day = datetime.strptime(f.stem[len(PREFIX):], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - day).days > keep_days:
            f.unlink(missing_ok=True)
            gone.append(f)
    return gone


class Writer:
    """Appends lines to the file of their day; one lock, because every container's reader writes here."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.lock = threading.Lock()
        self.last: dict[str, str] = {}  # container id -> Docker timestamp of the last line written
        folder.mkdir(parents=True, exist_ok=True)
        positions = folder / ".positions.json"
        if positions.exists():
            try:
                self.last = json.loads(positions.read_text())
            except ValueError:
                self.last = {}

    def write(self, container: str, service: str, raw: str) -> None:
        when, text = parse_line(raw)
        when = when or datetime.now(UTC)
        with self.lock:
            with open(file_for(when.date(), self.folder), "a", encoding="utf-8") as f:
                f.write(format_line(when, service, text))
            self.last[container] = raw.partition(" ")[0]

    def save_positions(self) -> None:
        with self.lock:
            (self.folder / ".positions.json").write_text(json.dumps(self.last))


class _UnixConnection(http.client.HTTPConnection):  # pragma: no cover - needs the Docker socket
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)


def _get(path: str, timeout: float | None = 30) -> http.client.HTTPResponse:  # pragma: no cover
    conn = _UnixConnection("docker", timeout=timeout)
    conn.request("GET", path)
    return conn.getresponse()


def containers() -> list[dict]:  # pragma: no cover - needs the Docker socket
    filters = urllib.parse.quote(json.dumps({"label": [f"com.docker.compose.project={PROJECT}"]}))
    return json.loads(_get(f"/containers/json?filters={filters}").read())


def follow(container: dict, writer: Writer) -> None:  # pragma: no cover - needs the Docker socket
    cid = container["Id"]
    service = container.get("Labels", {}).get("com.docker.compose.service", cid[:12])
    tty = json.loads(_get(f"/containers/{cid}/json").read())["Config"].get("Tty", False)
    since = writer.last.get(cid)
    start = (parse_line(since + " x")[0] + timedelta(microseconds=1)).timestamp() if since else time.time()
    resp = _get(f"/containers/{cid}/logs?follow=1&stdout=1&stderr=1&timestamps=1&since={start:.6f}", timeout=None)
    buffer, text = b"", ""
    while True:
        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
        if not chunk:
            return  # the container stopped; a new one is picked up by the main loop
        if tty:
            frames, buffer = [buffer + chunk], b""
        else:
            frames, buffer = demux(buffer + chunk)
        text += b"".join(frames).decode("utf-8", "replace")
        *lines, text = text.split("\n")
        for line in lines:
            if line.strip():
                writer.write(cid, service, line)


def main() -> None:  # pragma: no cover - process entry point
    writer = Writer(LOG_DIR)
    watched: dict[str, threading.Thread] = {}
    pruned_on: date | None = None
    print(f"log collector: writing {file_for(datetime.now(UTC).date())} (keeping {KEEP_DAYS} days)", flush=True)
    while True:
        try:
            for c in containers():
                service = c.get("Labels", {}).get("com.docker.compose.service", "")
                alive = watched.get(c["Id"])
                if service == SELF or (alive is not None and alive.is_alive()):
                    continue
                watched[c["Id"]] = threading.Thread(target=follow, args=(c, writer), daemon=True)
                watched[c["Id"]].start()
        except OSError as ex:
            print(f"log collector: Docker not reachable ({type(ex).__name__}); retrying", flush=True)
        writer.save_positions()
        today = datetime.now(UTC).date()
        if pruned_on != today:
            prune(LOG_DIR, KEEP_DAYS, today)
            pruned_on = today
        time.sleep(5)


if __name__ == "__main__":  # pragma: no cover
    main()
