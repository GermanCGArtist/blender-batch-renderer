//! Minimal egui desktop window: job list with status, Start / Stop / Pause,
//! and a live log tail. The render session runs on worker threads; the GUI
//! only reads shared state, so rendering never blocks the window.

use crate::jobqueue::JobQueue;
use crate::runner::{run_session, LiveState, RunConfig};
use eframe::egui;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

pub fn run_gui() -> anyhow::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([900.0, 600.0])
            .with_title("Blender Batch Renderer"),
        ..Default::default()
    };
    eframe::run_native(
        "bbr",
        options,
        Box::new(|_cc| Ok(Box::new(BbrApp::new()))),
    )
    .map_err(|e| anyhow::anyhow!("gui error: {e}"))?;
    Ok(())
}

struct BbrApp {
    queue: Arc<Mutex<JobQueue>>,
    stop: Arc<AtomicBool>,
    pause: Arc<AtomicBool>,
    running: Arc<AtomicBool>,
    live: Arc<Mutex<LiveState>>,
    session: Arc<Mutex<Option<JoinHandle<()>>>>,
    workers: usize,
}

impl BbrApp {
    fn new() -> Self {
        let queue = Arc::new(Mutex::new(JobQueue::load()));
        BbrApp {
            queue,
            stop: Arc::new(AtomicBool::new(false)),
            pause: Arc::new(AtomicBool::new(false)),
            running: Arc::new(AtomicBool::new(false)),
            live: Arc::new(Mutex::new(LiveState::default())),
            session: Arc::new(Mutex::new(None)),
            workers: 1,
        }
    }

    fn start_session(&mut self) {
        if self.running.load(Ordering::Relaxed) {
            return;
        }
        self.stop.store(false, Ordering::Relaxed);
        self.pause.store(false, Ordering::Relaxed);
        self.live.lock().unwrap().lines.clear();
        self.running.store(true, Ordering::Relaxed);
        let queue = self.queue.clone();
        let stop = self.stop.clone();
        let pause = self.pause.clone();
        let live = self.live.clone();
        let running = self.running.clone();
        let cfg = RunConfig {
            workers: self.workers,
            blender: "auto".to_string(),
            stop_after_job: false,
        };
        let handle = std::thread::spawn(move || {
            let _ = run_session(queue, &cfg, &stop, &pause, Some(live));
            running.store(false, Ordering::Relaxed);
        });
        *self.session.lock().unwrap() = Some(handle);
    }

    fn stop_session(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.pause.store(false, Ordering::Relaxed);
    }
}

impl eframe::App for BbrApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        egui::TopBottomPanel::top("controls").show(ctx, |ui| {
            ui.horizontal(|ui| {
                let running = self.running.load(Ordering::Relaxed);
                ui.label(format!(
                    "root: {}",
                    self.queue.lock().unwrap().root
                ));
                ui.separator();
                ui.add(egui::Slider::new(&mut self.workers, 1..=16).text("workers"));
                if ui
                    .add_enabled(!running, egui::Button::new("Start"))
                    .clicked()
                {
                    self.start_session();
                }
                if ui
                    .add_enabled(running, egui::Button::new("Stop"))
                    .clicked()
                {
                    self.stop_session();
                }
                let paused = self.pause.load(Ordering::Relaxed);
                if ui
                    .add_enabled(running, egui::Button::new(if paused { "Resume" } else { "Pause" }))
                    .clicked()
                {
                    self.pause.store(!paused, Ordering::Relaxed);
                }
            });
        });
        egui::BottomPanel::bottom("log").show(ctx, |ui| {
            ui.heading("Log");
            egui::ScrollArea::vertical()
                .max_height(200.0)
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    let live = self.live.lock().unwrap();
                    let tail: Vec<&String> =
                        live.lines.iter().rev().take(300).collect();
                    for line in tail.into_iter().rev() {
                        ui.monospace(line);
                    }
                });
        });
        egui::CentralPanel::default().show(ctx, |ui| {
            let jobs: Vec<(String, String, usize, usize)> = {
                let q = self.queue.lock().unwrap();
                q.load_jobs()
                    .into_iter()
                    .map(|j| {
                        (
                            j.name,
                            j.status.as_str().to_string(),
                            j.frames_done(),
                            j.frame_numbers().len(),
                        )
                    })
                    .collect()
            };
            egui::Grid::new("jobs").striped(true).show(ui, |ui| {
                ui.strong("job");
                ui.strong("status");
                ui.strong("frames");
                ui.end_row();
                for (name, status, done, total) in jobs {
                    ui.label(name);
                    ui.label(status);
                    ui.label(format!("{}/{}", done, total));
                    ui.end_row();
                }
            });
        });
        if self.running.load(Ordering::Relaxed) {
            ctx.request_repaint_after(std::time::Duration::from_millis(500));
        }
    }
}