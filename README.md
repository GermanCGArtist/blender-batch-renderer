# Blender Batch Renderer

This app renders a queue of `.blend` files one after another, for example overnight. You can stop, cancel or quit at any time, and it continues from the last finished frame. Files from different Blender versions can go in the same batch.

## Setup (once)

**macOS:** double-click `setup.command`. If macOS says it can't be opened, right-click it → Open.
**Windows:** run `setup.bat`. **Linux:** run `./setup.sh`.

The setup script creates a fresh private Python environment in `.venv` (about 0.5 GB) and installs PySide6-Essentials (the window, without the optional Qt add-ons), openpyxl (the Excel report), psutil (memory readings) and zstandard (reading compressed .blend files). It needs Python 3.9 or newer.

## Start

**macOS app (recommended):** double-click `build_app.command` once. It builds **Blender Batch Renderer.app**, with its own icon and Dock name, next to the script. Drag the app to Applications if you like. Re-run the script after updating the code.

The build leaves out the parts of Qt the app doesn't use (QML/Quick, PDF, networking, OpenGL, SVG, translations …; see `bbr.spec`). It then starts the new app once in the background as a test. If the trimmed app doesn't start, it automatically rebuilds with all of Qt included and says so. Once the app is built, `.venv` is only needed to run `run.command` or to rebuild, so you can delete it to save space; `setup.command` recreates it.

**Without building:** double-click `run.command` (or `run.bat` / `./run.sh`). A Terminal window opens alongside; keep it open while the app runs.

Only one copy of the app can run at a time. A second copy refuses to start instead of rendering the same queue twice.

On first launch the app looks for Blender installs in `/Applications` (or Program Files on Windows). You can add more under **Settings…**.

## Using it

1. **Add .blend…**, or drop files onto the list (dropping them onto a folder puts them in that folder). Pick a **render folder** (for example your project's `render` folder). Each file gets its own subfolder inside it.
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

**Folders:** use **New folder…** to group jobs, for example per project. Drag jobs between folders, or right-click a job → *Move to folder*. The queue renders top to bottom: jobs outside folders first, then each folder in order. Untick a job or a whole folder to skip it without removing it. A folder can have its own render folder, which is pre-filled when you add files to it.

**Blender version per file:** "Auto" reads the version from the .blend file's header. It uses the matching install, or the next newer one if there's no match. You can pick a specific install per job instead.

**Retries:** if Blender crashes or errors on a frame, the frame is tried again, up to *Attempts per frame* times, with an optional *Wait before retry*. After the last attempt the frame is marked failed and the job continues with the next frame. A file that can't even load is retried the same number of times, then the job is marked failed and the batch moves on. *Frame timeout* kills a frame that takes longer than that limit, and it counts as a failed attempt.

**Restart Blender every N frames** (job setting, off by default): starts a fresh Blender process every N frames. Blender's memory can creep up over a long sequence; restarting avoids crashes from that, at the cost of loading the file again.

**Safety checks:**
- **Disk space:** if the render drive has less free space than the limit in Settings (default 5 GB), or Blender reports "disk full", the batch stops after the current frame and keeps everything rendered so far. No frame is marked as failed. Free some space and press Start.
- **Changed .blend:** if a .blend is saved again after some of its frames were rendered, the job is put **on hold** and the batch carries on with other jobs. Under *Job ▾* choose *Continue with changed file* (keep the finished frames) or *Start job over*.
- **Missing .blend:** the job goes on hold instead of failing.
- **Moved render folders:** frames are tracked relative to the job folder, so moving or renaming the render folder or the drive is fine. Put the job back with *File → Add existing job folder…*.

**Keep awake:** while a batch runs, the computer is kept from sleeping (`caffeinate` on macOS). This only works while the Mac is plugged in, and a MacBook still sleeps when its lid is closed without an external display.

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
- **Live progress:** Blender 5 no longer prints its progress lines in background mode. The render script therefore forwards Blender's render statistics (samples, remaining time, memory) to the app.
- **Memory:** *Blender peak* is what Blender reports; *process peak* is the operating system's measurement of the Blender process. On Apple Silicon, GPU memory is shared with the system, so most of it shows up in the process figure.
- **Unbaked simulations** (cloth, particles, rigid body …) can come out different when a job resumes mid-sequence. Bake them first; the optional *Check the file first* setting warns about this.
- **Thumbnails** work for PNG/JPG/TIFF, not for EXR.

## Command line (no window)

```
.venv/bin/python -m bbr scan                 # find Blender installs
.venv/bin/python -m bbr add shot.blend --root /path/to/render --start 1 --end 100 --folder "Project X"
.venv/bin/python -m bbr list
.venv/bin/python -m bbr run                  # Ctrl+C = stop now
```

## Tests

`python3 tests/test_engine.py` runs the engine against a simulated Blender (`tests/fake_blender.py`). The tests cover crashes, retries, stop/resume, quitting mid-render, broken files, video→PNG switching, overrides, Blender 5-style output, Blender restarts every N frames, a full disk, changed and missing .blend files, moved render folders, folders and the single-instance lock.

`python3 tools/make_icon.py` redraws the app icon.

## License

MIT — see [LICENSE](LICENSE). You may use, change, share and sell this software; just keep the copyright notice.

The app window uses PySide6 (Qt), which is licensed under the LGPL. If you distribute a built app, the LGPL terms apply to the bundled Qt libraries.
