//! Running a Blender process, streaming its console line by line into a log
//! file, applying a timeout and killing it cleanly on stop.

use anyhow::{Context, Result};
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant};

pub struct RunOutcome {
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub killed: bool,
}

/// Spawn cmd, stream stdout+stderr line by line (each line passed to on_line
/// and appended to log_path), until the process exits, the timeout hits or the
/// stop flag goes up (then the process is killed).
pub fn run_blender(
    cmd: &[String],
    log_path: &Path,
    timeout_min: u64,
    stop: &AtomicBool,
    mut on_line: impl FnMut(&str),
) -> Result<RunOutcome> {
    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut log = File::create(log_path)
        .with_context(|| format!("cannot create log {}", log_path.display()))?;

    let mut command = Command::new(&cmd[0]);
    command
        .args(&cmd[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        // keep Windows from opening a console window per Blender run
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }

    let mut child: Child = command
        .spawn()
        .with_context(|| format!("cannot start {}", cmd[0]))?;

    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let (tx, rx) = mpsc::channel::<String>();
    let mut streams: Vec<Box<dyn std::io::Read + Send>> =
        vec![Box::new(stdout), Box::new(stderr)];
    for stream in streams.drain(..) {
        let tx = tx.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stream);
            for line in reader.lines().map_while(|l| l.ok()) {
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
    }
    drop(tx);

    let started = Instant::now();
    let deadline = if timeout_min > 0 {
        Some(started + Duration::from_secs(timeout_min * 60))
    } else {
        None
    };
    let mut timed_out = false;
    let mut killed = false;

    loop {
        // drain available lines without blocking
        while let Ok(line) = rx.try_recv() {
            on_line(&line);
            let _ = writeln!(log, "{}", line);
        }
        if stop.load(Ordering::Relaxed) {
            let _ = child.kill();
            killed = true;
            break;
        }
        if let Some(dl) = deadline {
            if Instant::now() >= dl {
                let _ = child.kill();
                timed_out = true;
                break;
            }
        }
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(_) => break,
        }
    }
    // flush whatever is still buffered from the readers
    std::thread::sleep(Duration::from_millis(200));
    while let Ok(line) = rx.try_recv() {
        on_line(&line);
        let _ = writeln!(log, "{}", line);
    }
    let _ = log.flush();

    let exit_code = match child.try_wait() {
        Ok(Some(status)) => status.code(),
        _ => {
            let _ = child.wait();
            None
        }
    };
    Ok(RunOutcome {
        exit_code,
        timed_out,
        killed,
    })
}