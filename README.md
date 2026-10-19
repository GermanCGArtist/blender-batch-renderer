# Blender Batch Renderer

This app renders a queue of `.blend` files one after another, for example overnight. You can stop, cancel or quit at any time, and it continues from the last finished frame. Files from different Blender versions can go in the same batch.

## Setup (once)

**macOS:** double-click `setup.command`. If macOS says it can't be opened, right-click it → Open.
**Windows:** run `setup.bat`. **Linux:** run `./setup.sh`.

The setup script creates a private Python environment in `.venv` and installs PySide6 (the window), openpyxl (the Excel report), psutil (memory readings) and zstandard (reading compressed .blend files). It needs Python 3.9 or newer.

## Start

Double-click `run.command` (or `run.bat` / `./run.sh`). The Terminal window that opens shows the app's own log. Keep it open while the app runs.

On first launch the app looks for Blender installs in `/Applications` (or Program Files on Windows). You can add more under **Settings…**.

## Using it

1. **Add .blend…**, or drop files onto the window. Pick a **render folder** (for example your project's `render` folder). Each file gets its own subfolder inside it.
2. Optional overrides: start/end frame, samples, resolution %. Everything else is taken from the .blend file as saved.
3. **▶ Start.**

| Button | What happens |
|---|---|
| **Stop after frame** | The current frame finishes, then the batch stops. |
| **■ Stop now** | Blender is killed immediately. The interrupted frame is rendered again next time. |
| **Cancel job** | The current job is marked cancelled; the batch continues with the next job. Finished frames are kept. |
| **Quit** (closing the window) | Asks: stop now, or finish this frame first. Either way, the next **Start** continues where it stopped. |
| **Retry failed frames** | Queues the job again and renders only frames that gave up. |
| **Start job over…** | Forgets the job's progress and renders every frame again. |
| **Create preview** | Builds an MP4 from the frames finished so far. This also happens automatically when a job finishes. |

**Blender version per file:** "Auto" reads the version from the .blend file's header. It uses the matching install, or the next newer one if there's no match. You can pick a specific install per job instead.

**Retries:** if Blender crashes or errors on a frame, the frame is tried again, up to *Attempts per frame* times, with an optional *Wait before retry*. After the last attempt the frame is marked failed and the job continues with the next frame. A file that can't even load is retried the same number of times, then the job is marked failed and the batch moves on. *Frame timeout* kills a frame that takes longer than that limit, and it counts as a failed attempt.

**Keep awake:** while a batch runs, the computer is kept from sleeping (`caffeinate` on macOS).

## Folder per job

```
<render folder>/<file name>/
  frames/            <name>_0001.png …  (+ frames/<node>/ for compositor File Output nodes)
  preview/           <name>.mp4
  data/              frames.csv     – one row per frame, written live
                     attempts.csv   – one row per Blender run
                     report.xlsx    – summary + charts (time, memory, file size per frame)
                     preflight.json – result of the optional file check
  logs/              attempt_001.log …  full Blender console of every run
  logs/crashes/      logs and Blender crash reports of failed runs
  job.json           the job's state – this is what makes resuming work
```

The queue itself lives in `~/Library/Application Support/BlenderBatchRenderer/` (on Windows `%APPDATA%\BlenderBatchRenderer`). If you remove a job and later add the same file with the same render folder, the app re-attaches to that folder and keeps the finished frames.

## Things to know

- **Frames can't be resumed halfway.** A frame interrupted mid-render starts from zero next time; finished frames are never rendered again.
- **Each frame is written to a temporary name and renamed once complete,** so a crash never leaves a broken frame behind. Frames that disappear from disk are rendered again automatically.
- **Video output is switched to a PNG sequence** (keeping 8/16-bit depth), because a half-written video can't be resumed. All other output settings stay as saved in the file.
- **The preview MP4** uses the scene's fps and resolution. Failed frames show as black gaps. EXR sequences use the scene's view transform; PNG/JPG are shown as they are.
- **Memory:** *Blender peak* is what Blender reports; *process peak* is the operating system's measurement of the Blender process. On Apple Silicon, GPU memory is shared with the system, so most of it shows up in the process figure.
- **Unbaked simulations** (cloth, particles, rigid body …) can come out different when a job resumes mid-sequence. Bake them first; the optional *Check the file first* setting warns about this.
- **Thumbnails** work for PNG/JPG/TIFF, not for EXR.

## Command line (no window)

```
.venv/bin/python -m bbr scan                 # find Blender installs
.venv/bin/python -m bbr add shot.blend --root /path/to/render --start 1 --end 100
.venv/bin/python -m bbr list
.venv/bin/python -m bbr run                  # Ctrl+C = stop now
```

## Tests

`python3 tests/test_engine.py` runs the engine against a simulated Blender (`tests/fake_blender.py`). The tests cover crashes, retries, stop/resume, quitting mid-render, broken files, video→PNG switching and overrides.
