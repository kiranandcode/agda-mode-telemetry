#!/usr/bin/env python3
"""Extract and display Agda mode telemetry from .agda-telemetry.db files."""

import argparse
import sqlite3
import sys
from pathlib import Path


def find_db(path: str) -> Path:
    p = Path(path)
    if p.is_file() and p.suffix == ".db":
        return p
    if p.is_dir():
        db = p / ".agda-telemetry.db"
        if db.exists():
            return db
    if p.is_file():
        db = p.parent / ".agda-telemetry.db"
        if db.exists():
            return db
    print(f"No .agda-telemetry.db found at {path}", file=sys.stderr)
    sys.exit(1)


def cmd_dump(args):
    db = find_db(args.path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM events"
    params = []
    clauses = []

    if args.session:
        clauses.append("session_id = ?")
        params.append(args.session)
    if args.command:
        clauses.append("command = ?")
        params.append(args.command)
    if args.file:
        clauses.append("file = ?")
        params.append(args.file)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY timestamp"
    if args.limit:
        query += f" LIMIT {args.limit}"

    rows = conn.execute(query, params).fetchall()
    for row in rows:
        parts = [
            row["timestamp"],
            row["command"],
            row["file"],
            f"L{row['line']}:C{row['col']}",
        ]
        if row["goal_number"] is not None:
            parts.append(f"goal={row['goal_number']}")
            if row["goal_content"]:
                content = row["goal_content"].strip()
                if content:
                    parts.append(f'"{content}"')
        print("  ".join(parts))

    conn.close()


def cmd_sessions(args):
    db = find_db(args.path)
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        """SELECT session_id,
                  MIN(timestamp) AS start,
                  MAX(timestamp) AS end,
                  COUNT(*) AS events,
                  GROUP_CONCAT(DISTINCT file) AS files
           FROM events
           GROUP BY session_id
           ORDER BY start"""
    ).fetchall()
    for sid, start, end, count, files in rows:
        print(f"{sid}  {start} -> {end}  ({count} events)  files: {files}")
    conn.close()


def cmd_summary(args):
    db = find_db(args.path)
    conn = sqlite3.connect(str(db))
    print("=== Command frequency ===")
    for cmd, count in conn.execute(
        "SELECT command, COUNT(*) AS c FROM events GROUP BY command ORDER BY c DESC"
    ):
        print(f"  {count:5d}  {cmd}")

    print("\n=== Events per file ===")
    for f, count in conn.execute(
        "SELECT file, COUNT(*) AS c FROM events GROUP BY file ORDER BY c DESC"
    ):
        print(f"  {count:5d}  {f}")

    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events"
    ).fetchone()[0]
    print(f"\nTotal: {total} events across {sessions} sessions")
    conn.close()


def cmd_trace(args):
    """Show a chronological trace of a session with timing deltas."""
    db = find_db(args.path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    if args.session:
        sid = args.session
    else:
        row = conn.execute(
            "SELECT session_id FROM events ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No events found.", file=sys.stderr)
            sys.exit(1)
        sid = row["session_id"]
        print(f"Session: {sid}\n")

    rows = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp",
        (sid,),
    ).fetchall()

    prev_ts = None
    for row in rows:
        ts = row["timestamp"]
        if prev_ts:
            delta = _ts_delta(prev_ts, ts)
            delta_str = f"+{delta:6.1f}s"
        else:
            delta_str = "       "

        line = f"{delta_str}  {row['command']:40s}  {row['file']}:{row['line']}"
        if row["goal_number"] is not None:
            line += f"  [goal {row['goal_number']}]"
        print(line)
        prev_ts = ts

    conn.close()


def _ts_delta(a: str, b: str) -> float:
    return (_parse_ts(b) - _parse_ts(a)).total_seconds()


def _parse_ts(s: str):
    from datetime import datetime

    # Strip timezone suffix (e.g. +0000) — all timestamps are local
    s = s.split("+")[0].split("-" if "T" in s.split(".")[-1] else "NOSPLIT")[0]
    if "." in s:
        return datetime.strptime(s[:23], "%Y-%m-%dT%H:%M:%S.%f")
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to .agda-telemetry.db, a directory, or an .agda file",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_dump = sub.add_parser("dump", help="Dump raw events")
    p_dump.add_argument("--session", help="Filter by session ID")
    p_dump.add_argument("--command", help="Filter by command name")
    p_dump.add_argument("--file", help="Filter by filename")
    p_dump.add_argument("--limit", type=int, help="Max rows")

    sub.add_parser("sessions", help="List sessions")

    sub.add_parser("summary", help="Aggregate statistics")

    p_trace = sub.add_parser("trace", help="Chronological trace with timing")
    p_trace.add_argument("--session", help="Session ID (default: latest)")

    args = parser.parse_args()
    if args.cmd is None:
        args.cmd = "summary"

    {"dump": cmd_dump, "sessions": cmd_sessions, "summary": cmd_summary, "trace": cmd_trace}[
        args.cmd
    ](args)


if __name__ == "__main__":
    main()
