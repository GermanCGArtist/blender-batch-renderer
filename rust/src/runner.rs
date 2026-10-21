//! The render loop: N parallel workers pick jobs off the queue, render them
//! with Blender, track frames, retry failures and honour stop / pause.

use crate::installs::resolve_blender;
use crate::jobqueue::JobQueue;
use crate::models::{AttemptRecord, Job, Status};
use crate::process::run_blender;
use anyhow::Result;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct RunConfig {
    /// Number of Blender instances rendering at once.
    pub workers: usize,
    /// Blender executable or "auto".
    pub blender: String,
    /// Stop the whole session once the current jobs finish.
    pub stop_after_job: bool,
}

impl Default for RunConfig {
    fn default() -> Self {
        RunConfig {
            workers: 1,
            blender: "auto".to_string(),
            stop_after_job: false,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct SessionSummary {
    pub jobs_done: u32,
    pub jobs_failed: u32,
    pub frames: u32,
    pub failed: u32,
    pub render_secs: f64,
}

/// Shared state the GUI polls while a session runs.
#[derive(Clone, Default)]
pub struct LiveState {
    pub lines: Vec<String>,
}

pub fn fmt_duration(secs: f64) -> String {
    let s = secs as u64;
    if s < 3600 {
        format!("{}m {:02}s", s / 60, s % 60)
    } else {
        format!("{}h {:02}m {:02}s", s / 3600, (s % 3600) / 60, s % 60)
    }
}

/// Render everything in the queue that is runnable. Blocks until done.
pub fn run_session(
    queue: Arc<Mutex<JobQueue>>,
    cfg: &RunConfig,
    stop: &Arc<AtomicBool>,
    pause: &Arc<AtomicBool>,
    live: Option<Arc<Mutex<LiveState>>>,
) -> Result<SessionSummary> {
    let summary = Arc::new(Mutex::new(SessionSummary::default()));
    let blender = resolve_blender(&cfg.blender);
    let workers = cfg.workers.max(1);
    let cfg = cfg.clone();

    let mut handles = Vec::new();
    for worker_id in 0..workers {
        let queue = queue.clone();
        let summary = summary.clone();
        let stop = stop.clone();
        let pause = pause.clone();
        let live = live.clone();
        let blender = blender.clone();
        let cfg = cfg.clone();
        handles.push(std::thread::spawn(move || {
            worker_loop(
                worker_id,
                &queue,
                &cfg,
                &blender,
                &stop,
                &pause,
                &summary,
                live.as_ref(),
            );
        }));
    }
    for h in handles {
        let _ = h.join();
    }
    let s = summary.lock().unwrap().clone();
    Ok(s)
}

fn emit(live: Option<&Arc<Mutex<LiveState>>>, line: String) {
    if let Some(l) = live {
        let mut g = l.lock().unwrap();
        g.lines.push(line);
        if g.lines.len() > 2000 {
            let cut = g.lines.len() - 2000;
            g.lines.drain(0..cut);
        }
    }
}

fn worker_loop(
    worker_id: usize,
    queue: &Arc<Mutex<JobQueue>>,
    cfg: &RunConfig,
    blender: &PathBuf,
    stop: &Arc<AtomicBool>,
    pause: &Arc<AtomicBool>,
    summary: &Arc<Mutex<SessionSummary>>,
    live: Option<&Arc<Mutex<LiveState>>>,
) {
    loop {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        // claim the next runnable job
        let claimed = {
            let mut q = queue.lock().unwrap();
            let mut claimed = None;
            for folder in q.ordered_jobs() {
                if let Some(mut job) = Job::load(std::path::Path::new(&folder)) {
                    if job.status.runnable() {
                        job.mark_started();
                        if job.save().is_ok() {
                            claimed = Some(job);
                            break;
                        }
                    }
                }
            }
            claimed
        };
        let Some(mut job) = claimed else {
            return; // queue empty
        };
        emit(live, format!("[w{}] start {}", worker_id, job.name));
        let outcome = render_job(&mut job, blender, stop, pause, live);
        let render_secs = outcome.1;
        {
            let mut s = summary.lock().unwrap();
            s.frames += outcome.0.frames_done;
            s.failed += outcome.0.frames_failed;
            s.render_secs += render_secs;
            match job.status {
                Status::Done | Status::DoneErrors => s.jobs_done += 1,
                Status::Stopped => {}
                _ => s.jobs_failed += 1,
            }
        }
        emit(
            live,
            format!(
                "[w{}] {} {}: {} ({} missing frames)",
                worker_id,
                job.status.as_str(),
                job.name,
                fmt_duration(render_secs),
                outcome.0.frames_failed
            ),
        );
        let _ = job.save();
        if cfg.stop_after_job && !stop.load(Ordering::Relaxed) {
            return;
        }
    }
}

/// Render one job with retries. Returns (attempt summary, seconds spent).
/// The job's frames/attempts/status are updated; the caller saves it.
fn render_job(
    job: &mut Job,
    blender: &PathBuf,
    stop: &Arc<AtomicBool>,
    pause: &Arc<AtomicBool>,
    live: Option<&Arc<Mutex<LiveState>>>,
) -> (AttemptRecord, f64) {
    let started_wall = Instant::now();
    let max_attempts = job.settings.attempts.max(1);
    let mut last = AttemptRecord {
        index: job.attempts.len() as u32 + 1,
        started: Job::now(),
        ended: Job::now(),
        ok: false,
        exit_code: None,
        frames_done: 0,
        frames_failed: 0,
        log: String::new(),
    };

    for attempt in 1..=max_attempts {
        while pause.load(Ordering::Relaxed) && !stop.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_millis(200));
        }
        if stop.load(Ordering::Relaxed) {
            break;
        }
        if attempt > 1 && job.settings.delay_s > 0 {
            std::thread::sleep(Duration::from_secs(job.settings.delay_s));
        }
        last = render_attempt(job, blender, attempt, stop, live);
        job.push_attempt(last.clone());
        let _ = job.save();
        if last.ok {
            break;
        }
        if stop.load(Ordering::Relaxed) {
            break;
        }
    }

    job.finished = Some(Job::now());
    if stop.load(Ordering::Relaxed) {
        job.status = Status::Stopped;
        job.status_note = "stopped by user".to_string();
    } else if job.frames_missing().is_empty() {
        let retries_used = job.attempts.len() > 1;
        job.status = if retries_used {
            Status::DoneErrors
        } else {
            Status::Done
        };
        job.status_note = String::new();
    } else if job.attempts.len() >= max_attempts as usize {
        job.status = Status::Failed;
        job.status_note = format!("{} frames missing", job.frames_missing().len());
    } else {
        job.status = Status::Failed;
        job.status_note = "render failed".to_string();
    }
    (last, started_wall.elapsed().as_secs_f64())
}

fn build_command(job: &Job, blender: &PathBuf) -> Vec<String> {
    let s = &job.settings;
    let mut cmd: Vec<String> = vec![
        blender.to_string_lossy().to_string(),
        "-b".to_string(),
        job.blend.clone(),
    ];
    // engine / samples / resolution overrides
    let mut exprs: Vec<String> = Vec::new();
    if s.engine != "auto" && !s.engine.is_empty() {
        exprs.push(format!(
            "bpy.context.scene.render.engine='{}'",
            s.engine.replace('\'', "")
        ));
    }
    if s.samples > 0 {
        exprs.push(format!("bpy.context.scene.cycles.samples={}", s.samples));
    }
    if s.pct > 0 && s.pct != 100 {
        exprs.push(format!("bpy.context.scene.render.resolution_percentage={}", s.pct));
    }
    if !exprs.is_empty() {
        cmd.push("--python-expr".to_string());
        cmd.push(exprs.join(";"));
    }
    // output path with frame number placeholders
    let out = job
        .frames_dir()
        .join(format!("{}_####", job.name))
        .to_string_lossy()
        .to_string();
    cmd.push("-o".to_string());
    cmd.push(out);
    cmd.push("-F".to_string());
    cmd.push(s.format.clone());
    let (start, end) = if s.start <= s.end { (s.start, s.end) } else { (s.end, s.start) };
    cmd.extend([
        "-s".to_string(),
        start.to_string(),
        "-e".to_string(),
        end.to_string(),
        "-j".to_string(),
        s.step.to_string(),
        "-a".to_string(),
    ]);
    cmd
}

fn render_attempt(
    job: &mut Job,
    blender: &PathBuf,
    attempt: u32,
    stop: &Arc<AtomicBool>,
    live: Option<&Arc<Mutex<LiveState>>>,
) -> AttemptRecord {
    let log_path = job.logs_dir().join(format!("attempt_{:03}.log", attempt));
    let cmd = build_command(job, blender);
    emit(live, format!("{} attempt {}: {:?}", job.name, attempt, cmd));

    let outcome = run_blender(
        &cmd,
        &log_path,
        job.settings.timeout_min,
        stop,
        |_| {},
    );

    // count done frames from the log ("Fra:42 ... Saved: '...'" per frame)
    for (frame, secs) in parse_saved_frames(&log_path) {
        if job.frames.get(&frame).map(|r| r.status == "done") != Some(true) {
            job.record_frame_attempt(frame);
            job.record_frame_done(frame, secs);
        }
    }
    // make sure every frame of the range has a record
    for f in job.frame_numbers() {
        job.frames.entry(f).or_default();
    }

    let ok = !outcome.killed
        && !outcome.timed_out
        && outcome.exit_code == Some(0)
        && job.frames_missing().is_empty();
    let frames_missing = job.frames_missing().len() as u32;
    AttemptRecord {
        index: attempt,
        started: Job::now(),
        ended: Job::now(),
        ok,
        exit_code: outcome.exit_code,
        frames_done: job.frames_done() as u32,
        frames_failed: frames_missing,
        log: log_path.to_string_lossy().to_string(),
    }
}

/// Parse a Blender log for saved frames. Blender prints e.g.
///   Fra:42 Mem:... | Saved: '/path/frame_0042.png'
/// We match the Fra of each Saved line (Blender prints Fra on the same line).
fn parse_saved_frames(log_path: &std::path::Path) -> Vec<(i32, Option<f64>)> {
    let mut out = Vec::new();
    let Ok(text) = std::fs::read_to_string(log_path) else {
        return out;
    };
    for line in text.lines() {
        if !line.contains("Saved:") {
            continue;
        }
        if let Some(rest) = line.strip_prefix("Fra:") {
            let num: String = rest
                .chars()
                .take_while(|c| c.is_ascii_digit() || *c == '-')
                .collect();
            if let Ok(n) = num.parse::<i32>() {
                out.push((n, None));
            }
        }
    }
    out
}