# agda-mode (with telemetry)

Fork of Emacs' agda-mode that records every user interaction in a SQLite database.

Requires Emacs 29+ (built-in SQLite support).

## Design

When `agda2-mode` activates on an `.agda` file, the telemetry module:

1. Creates `.agda-telemetry.db` in the same directory as the file
2. Logs a `session-start` event with an initial buffer snapshot
3. Advises every command in `agda2-command-table` to record events
4. Captures buffer snapshots and Agda responses after each interaction round
5. Logs `session-end` with a final snapshot when the buffer is killed or Emacs exits

## Database schema

The system is implemented with two tables, `events`, `snapshots` and `responses`

### Schemas

- `events`: list of user actions:

One row per user action.

| Column            | Type       | Description                                                               |
|-------------------|------------|---------------------------------------------------------------------------|
| `id`              | INTEGER PK | Auto-incrementing event ID                                                |
| `session_id`      | TEXT       | Unique session identifier (`YYYYMMDDHHMMSS_hash`)                         |
| `timestamp`       | TEXT       | ISO 8601 with milliseconds (`2026-04-22T10:00:05.100+0000`)               |
| `command`         | TEXT       | Elisp command name (`agda2-load`, `agda2-give`, etc.)                     |
| `file`            | TEXT       | Filename (basename only, e.g. `Nat.agda`)                                 |
| `point`           | INTEGER    | Buffer position (character offset)                                        |
| `line`            | INTEGER    | Line number (1-indexed)                                                   |
| `col`             | INTEGER    | Column number (0-indexed)                                                 |
| `goal_number`     | INTEGER    | Goal number if cursor was in a goal, else NULL                            |
| `goal_content`    | TEXT       | Text inside the goal braces, else NULL                                    |
| `buffer_modified` | INTEGER    | 1 if buffer had unsaved changes, 0 otherwise                              |
| `extra`           | TEXT       | Unified diff of buffer changes caused by this command (NULL if no change) |

Synthetic events `session-start` and `session-end` mark session boundaries.

- `snapshots`: buffer text after interaction rounds that modify the buffer.

| Column     | Type       | Description                                      |
|------------|------------|--------------------------------------------------|
| `id`       | INTEGER PK | Auto-incrementing                                |
| `event_id` | INTEGER    | References `events.id`                           |
| `content`  | TEXT       | Complete buffer text after the command's effects |

A snapshot is stored for `session-start` (initial state),
`session-end` (final state), and after any command that changes the
buffer (load, give, refine, case split, etc.). Commands that don't
modify the buffer (navigation, show goals) have no snapshot.

- `responses`: Agda process responses received during each interaction round (highlighting responses excluded).

| Column          | Type       | Description                                                        |
|-----------------|------------|--------------------------------------------------------------------|
| `id`            | INTEGER PK | Auto-incrementing                                                  |
| `event_id`      | INTEGER    | References `events.id`                                             |
| `sequence`      | INTEGER    | Order within the round (0-indexed)                                 |
| `function_name` | TEXT       | Response handler (`agda2-goals-action`, `agda2-give-action`, etc.) |
| `args_text`     | TEXT       | S-expression of the response arguments                             |

## Python tools

Alongside the telemetry, as an example of what is possible with this data, we provide a couple of python scripts to visualise the data that has been recorded:

- telemetry-extract.py: Quick analysis of recorded data.

```
python telemetry-extract.py /path/to/dir summary     # command frequency, per-file counts
python telemetry-extract.py /path/to/dir sessions    # list all sessions
python telemetry-extract.py /path/to/dir trace       # chronological trace with timing
python telemetry-extract.py /path/to/dir dump        # raw events (--session, --command, --file, --limit)
```

- telemetry-replay.py: Reconstruct and replay proof development sessions.

```
python telemetry-replay.py /path/to/dir replay              # full trace with diffs
python telemetry-replay.py /path/to/dir replay --step       # interactive step-through
python telemetry-replay.py /path/to/dir replay --file X.agda  # filter to one file
python telemetry-replay.py /path/to/dir diff 3              # diff for event 3
python telemetry-replay.py /path/to/dir cat 5               # buffer state at event 5
python telemetry-replay.py /path/to/dir export -o out/      # each state as a file
python telemetry-replay.py /path/to/dir asciinema -o s.cast # asciicast v2 export
```

The `asciinema` subcommand produces one `.cast` file per edited file. Play with `asciinema play s.cast` or upload with `asciinema upload s.cast`.

All subcommands accept `--session` and `--file` for filtering.

## Build notes

The list of `.el` files is duplicated in `/src/agda-mode/Main.hs`, to instruct Emacs to compile these files.
The exception is `agda2-mode-pkg.el`, which is just meta-information about the mode and need not be compiled.
