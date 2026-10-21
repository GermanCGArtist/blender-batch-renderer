# bbr (Rust rewrite)

Rust rewrite of the Python Blender Batch Renderer. Same on-disk layout and job
state model, one small fast binary, no Python or Qt required.

Rendering speed is unchanged - Blender does the rendering. What improves:
startup time, memory use, install size, and log/queue handling for many
parallel workers.

## Build

    cargo build --release                 # CLI
    cargo build --release --features gui  # CLI + desktop window

## CLI

    bbr scan                                   # find Blender installs, save them
    bbr add shot.blend --start 1 --end 250     # add a job
    bbr add shot.blend --samples 128 --engine CYCLES --format PNG
    bbr list                                   # show the queue
    bbr run --workers 4                        # render; Ctrl+C = stop now
    bbr requeue shot --keep-frames             # queue a job again
    bbr report shot                            # frames.csv, attempts.csv, report.xlsx
    bbr gui                                    # desktop window (gui feature)

## Layout (unchanged from the Python version)

- App data (queue.json, installs.json):
  macOS:   ~/Library/Application Support/BlenderBatchRenderer
  Windows: %APPDATA%\BlenderBatchRenderer
  Linux:   $XDG_DATA_HOME/BlenderBatchRenderer
  Override with BBR_DATA_DIR.
- Each job: <render root>/<job>/ with frames/, preview/, data/,
  logs/attempt_NNN.log, and job.json (the source of truth for resuming).

## Mapping from the Python modules

| Python             | Rust              |
|--------------------|-------------------|
| bbr/models.py      | src/models.rs     |
| bbr/jobqueue.py    | src/jobqueue.rs   |
| bbr/process.py     | src/process.rs    |
| bbr/runner.py / scheduler.py / worker.py (local mode) | src/runner.rs |
| bbr/installs.py    | src/installs.rs  |
| bbr/reports.py     | src/reports.rs   |
| bbr/storage.py     | src/storage.rs   |
| bbr/cli.py         | src/cli.rs       |
| bbr/gui/ (PySide6) | src/gui.rs (egui, feature "gui") |

## Not ported yet

- Render farm coordination over the network (api.py / workerapi.py / worker.py remote mode)
- Automatic self-updater (updater.py) - use cargo builds / releases instead
- Preview video encoding and Blender crash-report collection
- Memory sampling of Blender processes (RSS) - planned behind the "mem" feature