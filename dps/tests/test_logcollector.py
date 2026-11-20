"""The daily app log file (dps.ops.logcollector): Docker's stream read right, one file per day, secrets masked."""

from __future__ import annotations

import struct
from datetime import UTC, date, datetime

from dps.ops.logcollector import Writer, demux, file_for, format_line, parse_line, prune


def frame(text: str, stream: int = 1) -> bytes:
    data = text.encode()
    return struct.pack(">BxxxI", stream, len(data)) + data


def test_docker_frames_are_split_and_a_partial_frame_waits_for_the_rest():
    whole = frame("2026-11-09T10:42:01.123456789Z first\n") + frame("second\n", 2)
    payloads, rest = demux(whole + frame("third\n")[:5])
    assert payloads == [b"2026-11-09T10:42:01.123456789Z first\n", b"second\n"] and len(rest) == 5


def test_lines_keep_their_time_and_service_and_lose_their_secrets():
    when, text = parse_line("2026-11-09T10:42:01.123456789Z POST /login password=Hunter22")
    assert when == datetime(2026, 11, 9, 10, 42, 1, 123456, tzinfo=UTC)
    line = format_line(when, "core", text)
    assert line == "2026-11-09 10:42:01.123Z core             POST /login password=***\n"
    assert parse_line("no timestamp here") == (None, "no timestamp here")
    assert parse_line("2026-11-09T10:42:01Z short")[0] == datetime(2026, 11, 9, 10, 42, 1, tzinfo=UTC)


def test_one_file_per_day_continues_after_a_restart_and_old_files_go(tmp_path):
    w = Writer(tmp_path)
    w.write("c1", "dps-api", "2026-11-09T23:59:59.900000000Z late line")
    w.write("c1", "dps-api", "2026-11-10T00:00:00.100000000Z next day")
    w.save_positions()
    assert file_for(date(2026, 11, 9), tmp_path).read_text().endswith("dps-api          late line\n")
    assert "next day" in file_for(date(2026, 11, 10), tmp_path).read_text()
    assert Writer(tmp_path).last == {"c1": "2026-11-10T00:00:00.100000000Z"}  # where to carry on after a restart

    old = file_for(date(2026, 10, 1), tmp_path)
    old.write_text("x")
    (tmp_path / "cts-dps-notes.log").write_text("not a daily file")
    assert prune(tmp_path, 30, date(2026, 11, 10)) == [old] and not old.exists()
    (tmp_path / ".positions.json").write_text("{broken")
    assert Writer(tmp_path).last == {}
