# Blender Batch Renderer

This app renders a queue of `.blend` files one after another, for example overnight. You can stop, cancel or quit at any time, and it continues from the last finished frame. Files from different Blender versions can go in the same batch.

## Setup (once)

**macOS:** double-click `setup.command`. If macOS says it can't be opened, right-click it → Open.
**Windows:** run `setup.bat`. **Linux:** run `./setup.sh`.

The setup script creates a fresh private Python environment in `.venv` (about 0.5 GB) and installs PySide6-Essentials (the window, without the optional Qt add-ons), openpyxl (the Excel report), psutil (memory readings) and zstandard (reading compressed .blend files). It needs Python 3.9 or newer.

## Start

**macOS app (recommended):** double-click `build_app.command` once. It builds **Blender Batch Renderer.app**, with its own icon and Dock name, next to the script. Drag the app to Applications if you like. You only need to build again when an update asks for it (see below).

The build leaves out the parts of Qt the app doesn't use (QML/Quick, PDF, networking, OpenGL, SVG, translations …; see `bbr.spec`). It then starts the new app once in the background as a test. If the trimmed app doesn't start, it automatically rebuilds with all of Qt included and says so. Once the app is built, `.venv` is only needed to run `run.command` or to rebuild, so you can delete it to save space; `setup.command` recreates it.

**Windows app:** run `build_windows.bat` once. It builds a folder **Blender Batch Renderer** containing `Blender Batch Renderer.exe`; pin it to the taskbar if you like.

**Updates without rebuilding:** *Help → Check for updates…* looks on GitHub for a newer version and installs only the app code (1–2 MB), next to the current version. Updates never happen by themselves, and never while something renders.
- **Channels:** **Stable** (the `release` branch) offers versions that were tried and approved. **Alpha** (the `main` branch) offers every change as soon as it's made; these are experimental and marked "alpha".
- **Checks:** every file is checked against GitHub's checksums before it's installed.
- **Going back:** *Use previous version* in the same window switches back. If a new version doesn't start, the app goes back to the previous one by itself and says so.
- **When you still need to rebuild:** only when a version needs a newer Python, Qt or library. The update window then says so instead of installing it.
- **Where updates live:** in the app data folder (`app/`), not inside the app, so the macOS app stays intact.
- **Mac and PC:** update both to the same version, because coordinator and workers must match.

**Without building:** double-click `run.command` (or `run.bat` / `./run.sh`). A Terminal window opens alongside; keep it open while the app runs.

Only one copy of the app can run at a time. A second copy refuses to start instead of rendering the same queue twice. If you double-click a queue file while the app is already running, the running app opens it.

On first launch the app looks for Blender installs in `/Applications` (or Program Files on Windows). You can add more under **Settings…**.

## Using it

Hover the mouse over any setting, button or column to see what it does.

1. **Add .blend…** (⌘I), or drop files onto the list (dropping them onto a folder puts them in that folder). By default, the output goes into a folder called **`render` next to each .blend file** (`shots/shot_010.blend` → `shots/render/shot_010/`). You can choose a **custom folder** instead. A queue folder can also have its own render folder.
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

**Queue files:** save the queue as a `.bbrqueue` file with **File → Save Queue** (⌘S) or *Save Queue As…* (⇧⌘S). Open it again with *Open Queue…* (⌘O), *Open Recent*, or by double-clicking it in Finder. *New Queue* (⌘N) starts an empty queue. A queue file holds the folders, the jobs in their order and their on/off switches. Each job's progress lives in its own job folder, so the same job shows its progress in every queue that contains it. Saving is manual: a dot in the window's close button (or "*" in the title) means unsaved changes. The app asks before replacing a queue with unsaved changes. The current queue is still kept when you quit, so nothing is lost if you forget to save. Queue files store paths relative to themselves too, so a project folder that contains the queue file and its render folders can be moved as a whole. You can't open or start a new queue while a render is running.

**Folders:** use **New folder…** (⇧⌘N) to group jobs, for example per project. Drag jobs between folders, or right-click a job → *Move to folder*. The queue renders top to bottom: jobs outside folders first, then each folder in order. Untick a job or a whole folder to skip it without removing it. A folder can have its own render folder, which is pre-filled when you add files to it.

**Keyboard shortcuts** (Help → Keyboard shortcuts, ⌘/):

| Key | Action |
|---|---|
| ⌫ / Delete | Remove the selected job or folder (asks first) |
| ⌘⌫ | Remove without asking |
| ↩ | Open the job's render folder (on a folder: folder settings) |
| Space | Play the preview video |
| ⌘↑ / ⌘↓ | Move up / down |
| ⌘R / ⇧⌘. / ⌘. | Start / stop after this frame / stop now |
| ⌘N / ⌘O / ⌘S / ⇧⌘S | New / open / save / save queue as |
| ⌘I / ⇧⌘N | Add .blend files / new folder |
| ⌘P / ⌘, | Create preview / settings |

The keys that act on a row only work while the queue list has the focus, so typing in a text field never removes a job.

**Blender version per file:** "Auto" reads the version from the .blend file's header. It uses the matching install, or the next newer one if there's no match. You can pick a specific install per job instead.

**Retries:** if Blender crashes or errors on a frame, the frame is tried again, up to *Attempts* times, with an optional *Retry delay*. After the last attempt the frame is marked failed and the job continues with the next frame. A file that can't even load is retried the same number of times, then the job is marked failed and the batch moves on. *Frame timeout* kills a frame that takes longer than that limit, and it counts as a failed attempt.

**Restart Blender** (job setting, off by default): starts a fresh Blender process every N frames. Blender's memory can creep up over a long sequence; restarting avoids crashes from that, at the cost of loading the file again.

**Safety checks:**
- **Disk space:** if the render drive has less free space than the limit in Settings (default 5 GB), or Blender reports "disk full", the batch stops after the current frame and keeps everything rendered so far. No frame is marked as failed. Free some space and press Start.
- **Changed .blend:** if a .blend is saved again after some of its frames were rendered, the job is put **on hold** and the batch carries on with other jobs. Under *Job ▾* choose *Continue with changed file* (keep the finished frames) or *Start job over*.
- **Missing .blend:** the job goes on hold instead of failing.
- **Moved render folders:** frames are tracked relative to the job folder, so moving or renaming the render folder or the drive is fine. Put the job back with *File → Add existing job folder…*.

**Keep awake:** while a batch runs, the computer is kept from sleeping (`caffeinate` on macOS). This only works while the Mac is plugged in, and a MacBook still sleeps when its lid is closed without an external display.

## Status page & API

While the app runs, it serves a **status page** that works in any browser: *Help → Status page*, or http://127.0.0.1:8642/. It shows the batch and per-job progress, the frame being rendered with Blender's remaining time, the last finished frame, the queue with its folders, and Blender's console. It also has Start / Stop after frame / Stop now buttons. It updates every two seconds.

**On your phone or another computer:** in *Settings → Status page & API*, tick *Home network access*. Then use *Copy link*. The link contains a key, so only devices that know it get in. Away from home the app is not reachable. For that, use a VPN such as Tailscale; don't open a port on your router.

**API:** the same server offers a JSON API (version 1) for other tools. Requests from the same computer need the header `X-BBR-Request: 1` for anything that changes something; other devices need `X-API-Key: <key>`.

| Request | Does |
|---|---|
| `GET /api/v1/status` | batch state, live frame info, estimates |
| `GET /api/v1/queue` | folders and jobs in render order |
| `GET /api/v1/jobs/<id>` | job details (settings, scene, failed frames, runs, frame times) |
| `GET /api/v1/jobs/<id>/thumbnail` | last finished frame |
| `GET /api/v1/console?since=N` | recent Blender console lines |
| `POST /api/v1/jobs` | add a job: `{"blend": "/path/shot.blend", "folder": "Project X", "render_root": "", "frame_start": 1, "frame_end": 100, "start": true}` |
| `POST /api/v1/batch/start` · `/batch/stop` `{"mode": "after_frame"\|"now"}` · `/batch/cancel-job` | control the batch |
| `POST /api/v1/jobs/<id>/retry` · `/reset` · `/preview` · `/enabled` `{"enabled": false}` | job actions |
| `DELETE /api/v1/jobs/<id>` | remove a job from the queue (files stay) |
| `GET /api/v1/machines` · `POST /api/v1/machines/<id>/enabled` · `DELETE /api/v1/machines/<id>` | computers taking part, with per-machine and combined statistics |

Example from a terminal: `curl -X POST -H "X-BBR-Request: 1" -d '{"blend": "/Users/me/shot.blend"}' http://127.0.0.1:8642/api/v1/jobs`

## Rendering on several computers

Several computers can render the frames of one animation together, e.g. a Windows PC with an NVIDIA GPU on LAN and a MacBook on Wi-Fi. One computer is the **coordinator**: it has the queue, and all frames, reports and previews end up in its job folders. The others run the app in **worker** mode. No shared folder is needed; the app transfers everything itself.

**Setup:**
1. On every computer, install the app, the Blender version(s) your files need, and the add-ons your scenes use (e.g. LensSim). Add-ons are not copied between machines.
2. On the coordinator, open the **Machines** tab and tick *Share jobs with workers*. It shows a 6-digit pairing code. Each code works for one pairing and then changes. Every worker gets its own key; right-click a machine → *Unpair and remove* revokes it, and that computer has to pair again to come back. Allow incoming connections when the firewall asks.
3. On each worker, switch **Mode** (top right) to **Worker**, press *Find* (or type the coordinator's address), enter the code and press *Pair*. Pairing is needed only once. After that, a worker connects only when you press **Connect** in its window. Tick *Connect on start* to have it connect by itself; **Disconnect** stops it (not while it's rendering).
4. Press **Start** on the coordinator.

**How it works:**
- **Job files.** Before workers render a job, the coordinator lists every file the job needs and writes copies of the .blend and its linked libraries (also libraries linked by libraries) with paths pointing to those files. Your originals aren't changed. Files are sent one by one and checked by checksum, in three groups:

  | Group | Examples | Sent |
  |---|---|---|
  | Scene | the .blend and linked libraries | before the first frame; only changed files when you save again |
  | Assets | textures, HDRIs, UDIM tiles, Alembic/USD, single VDBs, movies, fonts | once; kept on the worker by checksum and reused by every job that uses them |
  | Frame files | image sequences, VDB sequences, simulation/fluid caches | before the first frame; only the frames the job's range uses |

  After opening the job, the worker's Blender checks that every file the scene refers to is there before it renders anything. (A "Smart" mode that streams frame files frame by frame is built but switched off for now.)
- **Checks.** Every file is checksummed on the way in both directions. A damaged download is fetched again; a damaged frame upload is sent again. The coordinator remembers checksums, so big files are only read again when their size or date changes. Files that don't shrink by at least 10 % aren't compressed for transfer.
- **Version check.** Coordinator and workers must run the same app version. A worker with another version is refused with "update the worker to …" instead of failing halfway.
- **Consistency check** (job setting, on by default). Before a computer joins a job, it renders a frame that's already done, and the coordinator compares the two images in Blender. Noise differences between CPU and GPU average out; a missing add-on effect, other colours or missing textures don't. Computers whose frame looks different are left out of that job, and the test frame is kept in `data/consistency/` to look at. Untick the check to use them anyway. The check runs again when the job copy or that computer's device setting changes.
- **Is it worth it?** A computer doesn't get one of the last frames if the others would finish all remaining frames before it finishes that one, counting render time plus moving its frame files. This avoids a slow computer holding up the end of a job.
- **Unbaked simulations** can come out differently on another computer, so such jobs stay on the coordinator. Bake them to share the job.
- **Frames.** Workers render one frame at a time and keep Blender open in between, so faster machines simply do more frames. Each finished frame is uploaded right away, and the worker deletes its copy once the coordinator has confirmed it.
- **Who gets frames.** Frames only go to workers with the same Blender version (major.minor) and the add-ons the file uses. Add-ons are detected from node groups, custom properties and render handlers. The Machines tab says why a worker is idle. If a job's add-ons don't affect the image, tick *Ignore add-ons* in its job settings.
- **Engines.** Cycles, EEVEE and Workbench work. Cycles uses each machine's own GPU settings (OptiX/CUDA, Metal).
- **Devices** (job settings): for each computer choose *From file*, *GPU*, *CPU* or *Off*. Example: a scene with an NVIDIA-only add-on renders on the PC's GPU and on the Mac's CPU (or not at all on the Mac). A scene that needs lots of VRAM renders on the Mac's GPU and is switched off for the PC. *GPU* uses the GPU set in that computer's Blender preferences, or the first one it finds.
- **Pausing one computer**, e.g. to use the PC for something else while a job runs. Use the worker window, the Mac's Machines tab (select the computer or right-click it), or the status page:
  - **Pause after frame:** it finishes and delivers its current frame, then takes no more.
  - **Stop now:** it drops its frame right away, and the frame goes to another computer. Nothing counts as failed.
  - **Resume:** it takes frames again.

  The rest of the job carries on on the other computers.
- **Stop after frame:** every machine delivers its current frame first. **Stop now** and **Cancel job:** every machine drops its frame, and nothing counts as failed.
- **A worker vanishes** (sleep, Wi-Fi drop): after 45 s without a heartbeat, its frame goes back to the pool. **A worker crashes on a frame:** the failed attempt is counted against that machine, and the frame is preferably given to another one.
- **Pausing a worker** (e.g. to use the PC for something else): on the worker, *Pause after frame* finishes and delivers its current frame, *Stop now* drops it at once, and *Resume* takes frames again. The same three buttons are in the coordinator's **Machines** tab (select the computer, or right-click it) and on the status page. The worker's frames go to the other computers, and nothing counts as failed.
- **Locked while rendering:** the mode switch, sharing and the worker's settings. On a worker, use *Pause after frame* or *Stop now* instead.

**Statistics:** the **Machines** tab shows, per machine:
- state, current frame with samples and remaining time;
- device, Blender versions and add-ons;
- frames, average frame time and failures this batch, plus all-time totals.

Above the table are the combined figures and the speed of all machines together (frames per hour). The **Live** tab shows frames per machine for the selected job. `frames.csv` has a *machine* column, and `report.xlsx` has a **Machines** sheet with charts. The worker window shows its own session, all-time and per-job statistics. The status page has a Machines section too.

The worker keeps downloaded files in its app data folder (on Windows `%APPDATA%\BlenderBatchRenderer\worker_cache`), or in a folder you choose with *Folder…* in the worker window. The oldest files are deleted when *Limit* is reached. Choose a short folder path (e.g. `D:\bbr`) if your file names are long, because Windows limits the length of paths.

Command line worker: `python -m bbr worker --code 123456` (finds the coordinator by itself) or `python -m bbr worker --server 192.168.1.20`.

Third-party render engines aren't supported for sharing yet.

## Blender add-on: "Send to Batch Renderer"

`blender_addon/bbr_send_to_queue.py` adds the open file to the queue from inside Blender. Install it in Blender: *Edit → Preferences → Add-ons → ▾ → Install from Disk…*, choose the file, and enable it. Then use *Render → Send to Batch Renderer…* or the *Batch Renderer* panel under *Properties → Output*. You choose the render folder (next to the .blend, or custom), an optional queue folder, an optional frame range, and whether to start right away. Unsaved changes are saved first, because the batch renders the file as it is on disk. The add-on talks to the app through the API, so the app must be running. If you change the port in the app, change it in the add-on's preferences too.

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
- **Unbaked simulations** (cloth, particles, rigid body …) can come out different when a job resumes mid-sequence. Bake them first; the optional *Check file first* setting warns about this.
- **Thumbnails** work for PNG/JPG/TIFF, not for EXR.

## Command line (no window)

```
.venv/bin/python -m bbr scan                 # find Blender installs
.venv/bin/python -m bbr add shot.blend --root /path/to/render --start 1 --end 100 --folder "Project X"
.venv/bin/python -m bbr list
.venv/bin/python -m bbr run                  # Ctrl+C = stop now (the status page runs too)
```

## Tests

`python3 tests/test_engine.py` runs the engine against a simulated Blender (`tests/fake_blender.py`). The tests cover crashes, retries, stop/resume, quitting mid-render, broken files, video→PNG switching, overrides, Blender 5-style output, Blender restarts every N frames, a full disk, changed and missing .blend files, moved render folders, folders and the single-instance lock.

`python3 tests/test_updater.py` tests updates against a local stand-in for GitHub:
- checking, installing and starting the new version;
- the alpha channel;
- refusing damaged downloads and versions that need a newer app;
- falling back when a version doesn't start, and switching back and forth.

`python3 tests/test_transfer.py` tests the file transfer:
- Smart transfer with per-frame files and reuse across jobs;
- damaged downloads and uploads;
- the automatic switch to Full copy;
- the version check, the consistency check, the "is it worth it" rule and remembered checksums.

`python3 tests/test_distributed.py` simulates a coordinator with several workers. It covers:
- job copies with nested linked libraries and add-on detection;
- pairing and access checks, and network discovery;
- fast and slow workers, and a worker with a missing add-on;
- crashing workers and a worker that disappears;
- stop now and stop after frame;
- the coordinator rendering together with a worker.

`python3 tests/test_api.py` tests the API, queue files, the default render folder and the Blender add-on (with a stand-in for Blender's Python module).

`python3 tools/make_icon.py` redraws the app icon. `python3 docs/make_docs.py` rebuilds the documentation PDF.

## License

MIT — see [LICENSE](LICENSE). You may use, change, share and sell this software; just keep the copyright notice.
The app window uses PySide6 (Qt), which is licensed under the LGPL. If you distribute a built app, the LGPL terms apply to the bundled Qt libraries.
