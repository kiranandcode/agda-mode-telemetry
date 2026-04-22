#!/usr/bin/env python3
"""Replay Agda mode interaction traces from .agda-telemetry.db files.

Reconstructs buffer state at each step using stored snapshots and
shows diffs between consecutive states, Agda responses, and timing.

Usage:
    python telemetry-replay.py /path/to/dir                # auto replay latest session
    python telemetry-replay.py /path/to/dir --step         # interactive step-through
    python telemetry-replay.py /path/to/dir --event 5      # jump to event
    python telemetry-replay.py /path/to/dir --export out/  # export each state as a file
"""

import argparse
import difflib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# --- ANSI colors ---

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @classmethod
    def disable(cls):
        for attr in ("RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW",
                      "BLUE", "MAGENTA", "CYAN"):
            setattr(cls, attr, "")


# --- Data types ---

@dataclass
class Event:
    id: int
    session_id: str
    timestamp: str
    command: str
    file: str
    point: int
    line: int
    col: int
    goal_number: int | None
    goal_content: str | None
    buffer_modified: int
    extra: str | None  # diff text if buffer changed


@dataclass
class Snapshot:
    event_id: int
    content: str


@dataclass
class Response:
    event_id: int
    sequence: int
    function_name: str
    args_text: str | None


@dataclass
class Step:
    event: Event
    snapshot: Snapshot | None = None
    responses: list[Response] = field(default_factory=list)
    buffer_before: str | None = None
    buffer_after: str | None = None


# --- DB access ---

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


def load_session(db_path: Path, session_id: str | None = None,
                 all_sessions: bool = False) -> tuple[str, list[Step]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if all_sessions:
        session_id = "all"
        where_clause = ""
        where_params: list = []
    else:
        if session_id is None:
            row = conn.execute(
                "SELECT session_id FROM events ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if not row:
                print("No events found.", file=sys.stderr)
                sys.exit(1)
            session_id = row["session_id"]
        where_clause = "WHERE e.session_id = ?"
        where_params = [session_id]

    events = [
        Event(**dict(r))
        for r in conn.execute(
            f"SELECT * FROM events e {where_clause} ORDER BY e.id",
            where_params,
        )
    ]

    snapshot_map: dict[int, Snapshot] = {}
    for r in conn.execute(
        f"""SELECT s.* FROM snapshots s
            JOIN events e ON s.event_id = e.id
            {where_clause}""",
        where_params,
    ):
        snapshot_map[r["event_id"]] = Snapshot(
            event_id=r["event_id"], content=r["content"]
        )

    response_map: dict[int, list[Response]] = {}
    for r in conn.execute(
        f"""SELECT r.* FROM responses r
            JOIN events e ON r.event_id = e.id
            {where_clause}
            ORDER BY r.event_id, r.sequence""",
        where_params,
    ):
        resp = Response(
            event_id=r["event_id"],
            sequence=r["sequence"],
            function_name=r["function_name"],
            args_text=r["args_text"],
        )
        response_map.setdefault(r["event_id"], []).append(resp)

    conn.close()

    # Build steps with reconstructed per-file buffer states
    steps = []
    file_buffers: dict[str, str | None] = {}

    for ev in events:
        step = Step(
            event=ev,
            snapshot=snapshot_map.get(ev.id),
            responses=response_map.get(ev.id, []),
        )
        step.buffer_before = file_buffers.get(ev.file)

        if step.snapshot:
            file_buffers[ev.file] = step.snapshot.content
        step.buffer_after = file_buffers.get(ev.file)

        steps.append(step)

    return session_id, steps


def filter_steps_by_file(steps: list[Step], filename: str | None) -> list[Step]:
    if filename is None:
        return steps
    return [s for s in steps if s.event.file == filename]


def get_files_in_session(steps: list[Step]) -> list[str]:
    seen: dict[str, None] = {}
    for s in steps:
        seen.setdefault(s.event.file, None)
    return list(seen.keys())


# --- Display ---

def format_header(step_num: int, total: int, step: Step) -> str:
    ev = step.event
    parts = [
        f"{C.BOLD}{C.CYAN}[{step_num}/{total}]{C.RESET}",
        f"{C.BOLD}{ev.command}{C.RESET}",
        f"{C.DIM}{ev.file}:{ev.line}:{ev.col}{C.RESET}",
    ]
    if ev.goal_number is not None:
        parts.append(f"{C.YELLOW}goal {ev.goal_number}{C.RESET}")
        if ev.goal_content and ev.goal_content.strip():
            parts.append(f'{C.DIM}"{ev.goal_content.strip()}"{C.RESET}')
    return "  ".join(parts)


def format_timestamp(step: Step, prev_step: Step | None) -> str:
    ts = step.event.timestamp
    if prev_step:
        delta = ts_delta(prev_step.event.timestamp, ts)
        return f"{C.DIM}{ts}  (+{delta:.1f}s){C.RESET}"
    return f"{C.DIM}{ts}{C.RESET}"


def _parse_ts(s: str) -> datetime:
    # Strip timezone suffix (e.g. +0000) — all timestamps are local
    s = s.split("+")[0].split("-" if "T" in s.split(".")[-1] else "NOSPLIT")[0]
    if "." in s:
        return datetime.strptime(s[:23], "%Y-%m-%dT%H:%M:%S.%f")
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def ts_delta(a: str, b: str) -> float:
    return (_parse_ts(b) - _parse_ts(a)).total_seconds()


def format_diff(before: str | None, after: str | None, stored_diff: str | None) -> str:
    if stored_diff:
        return colorize_diff(stored_diff)

    if before is None or after is None or before == after:
        return ""

    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
    )
    return colorize_diff("\n".join(diff))


def colorize_diff(diff_text: str) -> str:
    if not diff_text.strip():
        return ""
    lines = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f"{C.BOLD}{line}{C.RESET}")
        elif line.startswith("@@"):
            lines.append(f"{C.CYAN}{line}{C.RESET}")
        elif line.startswith("+"):
            lines.append(f"{C.GREEN}{line}{C.RESET}")
        elif line.startswith("-"):
            lines.append(f"{C.RED}{line}{C.RESET}")
        else:
            lines.append(line)
    return "\n".join(lines)


def format_responses(responses: list[Response], verbose: bool = False) -> str:
    if not responses:
        return ""
    lines = [f"{C.MAGENTA}Agda responses:{C.RESET}"]
    for r in responses:
        line = f"  {C.DIM}{r.sequence}.{C.RESET} {r.function_name}"
        if verbose and r.args_text:
            args = r.args_text
            if len(args) > 200:
                args = args[:200] + "..."
            line += f" {C.DIM}{args}{C.RESET}"
        lines.append(line)
    return "\n".join(lines)


def print_step(step_num: int, total: int, step: Step, prev_step: Step | None,
               verbose: bool = False, show_buffer: bool = False):
    print(f"\n{'=' * 72}")
    print(format_header(step_num, total, step))
    print(format_timestamp(step, prev_step))

    diff_text = format_diff(step.buffer_before, step.buffer_after, step.event.extra)
    if diff_text:
        print(f"\n{diff_text}")

    resp_text = format_responses(step.responses, verbose)
    if resp_text:
        print(f"\n{resp_text}")

    if show_buffer and step.buffer_after:
        print(f"\n{C.DIM}--- buffer state ---{C.RESET}")
        for i, line in enumerate(step.buffer_after.splitlines(), 1):
            print(f"{C.DIM}{i:4d}{C.RESET}  {line}")


# --- Commands ---

def cmd_replay(args):
    if not sys.stdout.isatty():
        C.disable()

    session_id, all_steps = load_session(find_db(args.path), args.session,
                         getattr(args, "all", False))
    steps = filter_steps_by_file(all_steps, getattr(args, "file", None))
    if not steps:
        print("No events in session.", file=sys.stderr)
        return

    files = get_files_in_session(all_steps)
    print(f"{C.BOLD}Session:{C.RESET} {session_id}")
    print(f"{C.BOLD}Events:{C.RESET}  {len(steps)}")
    if len(files) > 1:
        print(f"{C.BOLD}Files:{C.RESET}   {', '.join(files)}")

    snapshot_count = sum(1 for s in steps if s.snapshot)
    print(f"{C.BOLD}Snapshots:{C.RESET} {snapshot_count}")

    start_idx = 0
    if args.event:
        start_idx = max(0, args.event - 1)

    if args.step:
        interactive_replay(steps, start_idx, args.verbose)
    else:
        for i, step in enumerate(steps[start_idx:], start_idx + 1):
            prev = steps[i - 2] if i > 1 else None
            print_step(i, len(steps), step, prev, args.verbose, args.buffer)
    print()


def interactive_replay(steps: list[Step], start: int, verbose: bool):
    idx = start
    while 0 <= idx < len(steps):
        step = steps[idx]
        prev = steps[idx - 1] if idx > 0 else None
        print_step(idx + 1, len(steps), step, prev, verbose, show_buffer=False)

        print(f"\n{C.DIM}[Enter]=next  [p]=prev  [b]=show buffer  "
              f"[r]=responses  [N]=jump to N  [q]=quit{C.RESET}")
        try:
            inp = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if inp == "" or inp == "n":
            idx += 1
        elif inp == "p":
            idx = max(0, idx - 1)
        elif inp == "b":
            if step.buffer_after:
                print(f"\n{C.DIM}--- buffer state ---{C.RESET}")
                for i, line in enumerate(step.buffer_after.splitlines(), 1):
                    print(f"{C.DIM}{i:4d}{C.RESET}  {line}")
            else:
                print(f"{C.DIM}(no buffer state available){C.RESET}")
        elif inp == "r":
            if step.responses:
                print(format_responses(step.responses, verbose=True))
            else:
                print(f"{C.DIM}(no responses){C.RESET}")
        elif inp == "q":
            break
        else:
            try:
                idx = int(inp) - 1
                idx = max(0, min(idx, len(steps) - 1))
            except ValueError:
                pass


def cmd_export(args):
    session_id, all_steps = load_session(find_db(args.path), args.session,
                         getattr(args, "all", False))
    steps = filter_steps_by_file(all_steps, getattr(args, "file", None))
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    for i, step in enumerate(steps, 1):
        if step.buffer_after:
            filename = step.event.file
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            out_path = out_dir / f"{i:04d}_{step.event.command}_{stem}{suffix}"
            out_path.write_text(step.buffer_after, encoding="utf-8")
            exported += 1

    # Also write the event log
    log_path = out_dir / "events.log"
    with open(log_path, "w") as f:
        for i, step in enumerate(steps, 1):
            ev = step.event
            f.write(f"{i:4d}  {ev.timestamp}  {ev.command:40s}  "
                    f"{ev.file}:{ev.line}:{ev.col}")
            if ev.goal_number is not None:
                f.write(f"  [goal {ev.goal_number}]")
            has_snap = "S" if step.snapshot else " "
            has_diff = "D" if (step.buffer_before != step.buffer_after
                               and step.buffer_before is not None) else " "
            f.write(f"  [{has_snap}{has_diff}]")
            f.write("\n")

    print(f"Exported {exported} buffer states and event log to {out_dir}/")


def cmd_diff(args):
    """Show the diff for a specific event."""
    if not sys.stdout.isatty():
        C.disable()

    session_id, all_steps = load_session(find_db(args.path), args.session,
                         getattr(args, "all", False))
    steps = filter_steps_by_file(all_steps, getattr(args, "file", None))
    idx = args.event - 1
    if idx < 0 or idx >= len(steps):
        print(f"Event {args.event} out of range (1-{len(steps)})", file=sys.stderr)
        sys.exit(1)

    step = steps[idx]
    prev = steps[idx - 1] if idx > 0 else None

    print(format_header(idx + 1, len(steps), step))
    diff_text = format_diff(step.buffer_before, step.buffer_after, step.event.extra)
    if diff_text:
        print(f"\n{diff_text}")
    else:
        print(f"\n{C.DIM}(no buffer changes){C.RESET}")

    if step.buffer_after:
        print(f"\n{C.DIM}--- full buffer ({len(step.buffer_after)} chars, "
              f"{len(step.buffer_after.splitlines())} lines) ---{C.RESET}")


def cmd_cat(args):
    """Print the buffer state at a specific event."""
    _, all_steps = load_session(find_db(args.path), args.session,
                         getattr(args, "all", False))
    steps = filter_steps_by_file(all_steps, getattr(args, "file", None))
    idx = args.event - 1
    if idx < 0 or idx >= len(steps):
        print(f"Event {args.event} out of range (1-{len(steps)})", file=sys.stderr)
        sys.exit(1)

    step = steps[idx]
    if step.buffer_after:
        sys.stdout.write(step.buffer_after)
    else:
        print(f"No buffer state available at event {args.event}", file=sys.stderr)
        sys.exit(1)


def cmd_asciinema(args):
    """Export session as asciicast v2 for asciinema playback.
    Produces one .cast file per file edited in the session."""
    session_id, all_steps = load_session(find_db(args.path), args.session,
                         getattr(args, "all", False))
    if not all_steps:
        print("No events in session.", file=sys.stderr)
        sys.exit(1)

    files = get_files_in_session(all_steps)
    if args.file:
        files = [f for f in files if f == args.file]
        if not files:
            print(f"No events for file '{args.file}'", file=sys.stderr)
            sys.exit(1)

    out_base = Path(args.output)
    # If single file, use the output path directly; otherwise use it as a stem
    single = len(files) == 1

    for filename in files:
        steps = filter_steps_by_file(all_steps, filename)
        if not steps:
            continue

        if single:
            out_path = out_base
        else:
            stem = Path(filename).stem
            out_path = out_base.with_stem(f"{out_base.stem}_{stem}")

        _write_cast(out_path, session_id, filename, steps, args)

    if not single:
        print(f"\nWrote {len(files)} cast files (one per edited file)")


def _write_cast(out_path: Path, session_id: str, filename: str,
                steps: list[Step], args):
    width = args.width
    height = args.height
    pause = args.pause

    cast_events = []
    t = 0.0

    title = f"Agda: {filename}  ({len(steps)} events)"
    cast_events.append(_cast_frame(t, _clear_screen(width, height)))
    cast_events.append(_cast_frame(t, _render_title(title, width)))
    t += 1.5

    prev_step = None
    for i, step in enumerate(steps):
        if prev_step:
            delta = ts_delta(prev_step.event.timestamp, step.event.timestamp)
            t += min(delta, pause)

        frame = _render_step_frame(i + 1, len(steps), step, prev_step, width, height)
        cast_events.append(_cast_frame(t, _clear_screen(width, height)))
        cast_events.append(_cast_frame(t + 0.05, frame))
        prev_step = step

    t += 2.0

    header = {
        "version": 2,
        "width": width,
        "height": height,
        "timestamp": int(_parse_ts(steps[0].event.timestamp).timestamp()),
        "title": f"{filename} — session {session_id}",
        "env": {"TERM": "xterm-256color"},
    }

    with open(out_path, "w") as f:
        f.write(json.dumps(header) + "\n")
        for ev in cast_events:
            f.write(json.dumps(ev) + "\n")

    print(f"Wrote {out_path} ({len(cast_events)} frames, {t:.1f}s)")
    print(f"  asciinema play {out_path}")


def _cast_frame(t: float, text: str) -> list:
    return [round(t, 4), "o", text]


def _clear_screen(width: int, height: int) -> str:
    return "\033[2J\033[H"


def _render_title(title: str, width: int) -> str:
    return f"\033[1;36m{'=' * width}\r\n{title}\r\n{'=' * width}\033[0m\r\n"


def _render_step_frame(step_num: int, total: int, step: Step,
                       prev_step: Step | None, width: int, height: int) -> str:
    lines = []
    ev = step.event

    # Header
    header = f"\033[1;36m[{step_num}/{total}]\033[0m  \033[1m{ev.command}\033[0m"
    header += f"  \033[2m{ev.file}:{ev.line}:{ev.col}\033[0m"
    if ev.goal_number is not None:
        header += f"  \033[33mgoal {ev.goal_number}\033[0m"
        if ev.goal_content and ev.goal_content.strip():
            header += f' \033[2m"{ev.goal_content.strip()}"\033[0m'
    lines.append(header)

    # Timestamp
    ts_line = f"\033[2m{ev.timestamp}"
    if prev_step:
        delta = ts_delta(prev_step.event.timestamp, ev.timestamp)
        ts_line += f"  (+{delta:.1f}s)"
    ts_line += "\033[0m"
    lines.append(ts_line)
    lines.append("")

    # Diff
    if step.buffer_before is not None and step.buffer_after is not None:
        if step.buffer_before != step.buffer_after:
            diff_src = step.event.extra
            if not diff_src:
                diff_src = "\n".join(difflib.unified_diff(
                    step.buffer_before.splitlines(),
                    step.buffer_after.splitlines(),
                    fromfile="before", tofile="after", lineterm="",
                ))
            if diff_src.strip():
                for dl in diff_src.splitlines():
                    if dl.startswith("+++") or dl.startswith("---"):
                        lines.append(f"\033[1m{dl}\033[0m")
                    elif dl.startswith("@@"):
                        lines.append(f"\033[36m{dl}\033[0m")
                    elif dl.startswith("+"):
                        lines.append(f"\033[32m{dl}\033[0m")
                    elif dl.startswith("-"):
                        lines.append(f"\033[31m{dl}\033[0m")
                    else:
                        lines.append(dl)
                lines.append("")

    # Responses
    if step.responses:
        lines.append("\033[35mAgda responses:\033[0m")
        for r in step.responses:
            lines.append(f"  \033[2m{r.sequence}.\033[0m {r.function_name}")
        lines.append("")

    # Buffer preview (last section, truncated to fit)
    if step.buffer_after:
        lines.append("\033[2m--- buffer ---\033[0m")
        buf_lines = step.buffer_after.splitlines()
        available = height - len(lines) - 1
        for bl in buf_lines[:max(available, 5)]:
            lines.append(f"\033[2m{bl}\033[0m")

    return "\r\n".join(lines)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path", nargs="?", default=".",
        help="Path to .agda-telemetry.db, a directory, or an .agda file",
    )
    sub = parser.add_subparsers(dest="cmd")

    file_help = "Filter to a specific filename (default: all files)"
    all_help = "Include all sessions (default: latest session only)"

    p_replay = sub.add_parser("replay", help="Replay interaction trace")
    p_replay.add_argument("--session", help="Session ID (default: latest)")
    p_replay.add_argument("--all", action="store_true", help=all_help)
    p_replay.add_argument("--file", help=file_help)
    p_replay.add_argument("--step", action="store_true",
                          help="Interactive step-through mode")
    p_replay.add_argument("--event", type=int,
                          help="Start at event number")
    p_replay.add_argument("--verbose", "-v", action="store_true",
                          help="Show full response args")
    p_replay.add_argument("--buffer", "-b", action="store_true",
                          help="Show full buffer at each step")

    p_export = sub.add_parser("export",
                              help="Export buffer states as individual files")
    p_export.add_argument("--session", help="Session ID (default: latest)")
    p_export.add_argument("--all", action="store_true", help=all_help)
    p_export.add_argument("--file", help=file_help)
    p_export.add_argument("--output", "-o", default="replay-export",
                          help="Output directory")

    p_diff = sub.add_parser("diff", help="Show diff for a specific event")
    p_diff.add_argument("event", type=int, help="Event number")
    p_diff.add_argument("--session", help="Session ID (default: latest)")
    p_diff.add_argument("--all", action="store_true", help=all_help)
    p_diff.add_argument("--file", help=file_help)

    p_cat = sub.add_parser("cat", help="Print buffer state at an event")
    p_cat.add_argument("event", type=int, help="Event number")
    p_cat.add_argument("--session", help="Session ID (default: latest)")
    p_cat.add_argument("--all", action="store_true", help=all_help)
    p_cat.add_argument("--file", help=file_help)

    p_ascii = sub.add_parser("asciinema",
                             help="Export as asciicast v2 (one .cast per file)")
    p_ascii.add_argument("--session", help="Session ID (default: latest)")
    p_ascii.add_argument("--all", action="store_true", help=all_help)
    p_ascii.add_argument("--file", help="Export only this file")
    p_ascii.add_argument("--output", "-o", default="session.cast",
                         help="Output .cast file (default: session.cast)")
    p_ascii.add_argument("--width", type=int, default=120,
                         help="Terminal width (default: 120)")
    p_ascii.add_argument("--height", type=int, default=40,
                         help="Terminal height (default: 40)")
    p_ascii.add_argument("--pause", type=float, default=5.0,
                         help="Max pause between events in seconds (default: 5)")

    args = parser.parse_args()
    if args.cmd is None:
        args.cmd = "replay"
        args.session = None
        args.all = False
        args.file = None
        args.step = False
        args.event = None
        args.verbose = False
        args.buffer = False

    dispatch = {
        "replay": cmd_replay,
        "export": cmd_export,
        "diff": cmd_diff,
        "cat": cmd_cat,
        "asciinema": cmd_asciinema,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
