# bbr — Rust rewrite

A Rust port of the Python Blender Batch Renderer. One small, fast, self-contained
binary — no Python, no Qt. The queue and job-state model are identical to the
Python version, so both tools can work on the same jobs.

> **Note on performance:** rendering speed itself is unchanged — Blender does the
> rendering either way. What improves: startup time, idle memory use, install
> size, and log/queue handling with many parallel workers.

## Build

```sh
cargo build --release                 # CLI only
cargo build --release --features gui  # CLI + desktop window (egui)
```

## CLI

```sh
bbr scan                                   # find Blender installs and save them
bbr add shot.blend --start 1 --end 250     # add a job to the queue
bbr add shot.blend --samples 128 --engine CYCLES --format PNG
bbr list                                   # show the queue and per-job status
bbr run --workers 4                        # render; Ctrl+C = stop now
bbr requeue shot --keep-frames             # put a job back in the queue
bbr remove shot                            # delete a job and its folder
bbr report shot                            # write frames.csv, attempts.csv, report.xlsx
bbr gui                                    # desktop window (build with --features gui)
```

## How it works

- One or more worker threads pick runnable jobs (queued / stopped) off the queue.
- Each job renders via `blender -b <file> -o <out> -s <start> -e <end> -j <step> -a`,
  with optional engine / samples / resolution overrides applied through `--python-expr`.
- Blender's console is streamed line by line into `logs/attempt_NNN.log`; frames
  are marked done from the `Fra:... Saved: ...` lines.
- Failed jobs retry up to `attempts` times with an optional delay and timeout.
- Ctrl+C / Stop kills the current Blender process; finished frames are kept and
  the job resumes where it left off.

## GUI

Build with `cargo build --release --features gui` and run `bbr gui` for the
desktop window — the same layout as the Python app:

- Toolbar: add .blend files (or drop them onto the window), new folder,
  remove, move up/down, Start, Stop after frame, Stop now, Pause, workers,
  Settings, Quit.
- Queue tree: folder groups, enable checkboxes, per-job status, progress,
  frames, ETA, and per-job actions (retry failed frames, start over,
  continue with changed file, open render folder / report, move to folder).
- Details panel: a Live tab (current frame and progress of the running job,
  job statistics, frame render times) and a Job settings tab with a persistent
  draft + Apply / Set as defaults.
- Bottom panel: Console (live Blender output) and Messages.
- Settings dialog: scan for Blender installs, add one manually, defaults for
  new jobs, workers.

Not ported (no backend in the Rust core yet): the Machines tab / network
rendering, Issues (preflight), preview encoding, status page, updater.

## On-disk layout (unchanged from the Python version)

- App data (`queue.json`, `installs.json`):
  - macOS: `~/Library/Application Support/BlenderBatchRenderer`
  - Windows: `%APPDATA%\BlenderBatchRenderer`
  - Linux: `$XDG_DATA_HOME/BlenderBatchRenderer` (or `~/.local/share/...`)
  - Override with `BBR_DATA_DIR`.
- Each job: `<render root>/<job>/` with `frames/`, `preview/`, `data/`,
  `logs/attempt_NNN.log`, and `job.json` — the source of truth for resuming.

## Module mapping

| Python             | Rust              |
|--------------------|-------------------|
| `bbr/models.py`      | `src/models.rs`     |
| `bbr/jobqueue.py`    | `src/jobqueue.rs`   |
| `bbr/process.py`     | `src/process.rs`    |
| `bbr/runner.py`, `scheduler.py`, `worker.py` (local mode) | `src/runner.rs` |
| `bbr/installs.py`    | `src/installs.rs`  |
| `bbr/reports.py`     | `src/reports.rs`   |
| `bbr/storage.py`     | `src/storage.rs`   |
| `bbr/cli.py`         | `src/cli.rs`       |
| `bbr/gui/` (PySide6)  | `src/gui/` (egui, feature `gui`) |

## Not ported yet

- Network render-farm coordination (`api.py` / `workerapi.py` / remote `worker.py`)
- Automatic self-updater (`updater.py`) — use cargo builds / releases instead
- Preview video encoding and Blender crash-report collection
- RSS memory sampling of Blender processes
