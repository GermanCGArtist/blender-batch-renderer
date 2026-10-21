//! Feature-complete egui desktop window, ported from the PySide6 GUI.
//!
//! Layout mirrors the Python app: toolbar on top, the queue tree in the
//! centre, a details panel (Live / Job settings) on the right and
//! Console / Messages at the bottom.
//!
//! Not ported (backend not in the Rust core yet): the Machines tab and
//! network rendering, Issues (preflight), preview encoding, status page.

mod dialogs;
mod job_form;
mod live;
mod queue;

use crate::jobqueue::JobQueue;
use crate::models::{Job, JobSettings, Status};
use crate::runner::{run_session, LiveState, RunConfig};
use eframe::egui;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

pub fn run_gui() -> anyhow::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1100.0, 700.0])
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

/// App-level settings, saved to <app data>/settings.json.
#[derive(Clone, Serialize, Deserialize)]
pub struct AppDefaults {
    pub workers: usize,
    pub blender: String,
    #[serde(default)]
    pub settings: JobSettings,
}

impl Default for AppDefaults {
    fn default() -> Self {
        AppDefaults {
            workers: 1,
            blender: "auto".to_string(),
            settings: JobSettings::default(),
        }
    }
}

fn defaults_file() -> PathBuf {
    crate::storage::app_data_dir().join("settings.json")
}

fn load_defaults() -> AppDefaults {
    crate::storage::read_json(&defaults_file())
        .and_then(|v| serde_json::from_value(v).ok())
        .unwrap_or_default()
}

fn save_defaults(d: &AppDefaults) {
    if let Ok(v) = serde_json::to_value(d) {
        let _ = crate::storage::atomic_write_json(&defaults_file(), &v);
    }
}

/// Open a path in the OS file manager / default app.
pub fn open_path(p: &Path) {
    let p = p.to_path_buf();
    std::thread::spawn(move || {
        if cfg!(target_os = "macos") {
            let _ = std::process::Command::new("open").arg(&p).spawn();
        } else if cfg!(windows) {
            let _ = std::process::Command::new("explorer").arg(&p).spawn();
        } else {
            let _ = std::process::Command::new("xdg-open").arg(&p).spawn();
        }
    });
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DetailsTab {
    Live,
    Settings,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum BottomTab {
    Console,
    Messages,
}

struct BbrApp {
    queue: Arc<Mutex<JobQueue>>,
    stop: Arc<AtomicBool>,
    pause: Arc<AtomicBool>,
    stop_after_frame: Arc<AtomicBool>,
    running: Arc<AtomicBool>,
    live: Arc<Mutex<LiveState>>,
    session: Option<JoinHandle<()>>,

    selected: Option<String>,
    follow: bool,
    messages: Vec<String>,
    tab: DetailsTab,
    bottom: BottomTab,

    defaults: AppDefaults,
    /// Draft settings per selected job: (job name, draft).
    draft: Option<(String, JobSettings)>,
    add_dialog: Option<dialogs::AddJobs>,
    /// Text buffer while the "New folder" dialog is open.
    new_folder: Option<String>,
    show_settings: bool,
    show_quit: bool,
}

impl BbrApp {
    fn new() -> Self {
        BbrApp {
            queue: Arc::new(Mutex::new(JobQueue::load())),
            stop: Arc::new(AtomicBool::new(false)),
            pause: Arc::new(AtomicBool::new(false)),
            stop_after_frame: Arc::new(AtomicBool::new(false)),
            running: Arc::new(AtomicBool::new(false)),
            live: Arc::new(Mutex::new(LiveState::default())),
            session: None,
            selected: None,
            follow: true,
            messages: Vec::new(),
            tab: DetailsTab::Live,
            bottom: BottomTab::Console,
            defaults: load_defaults(),
            draft: None,
            add_dialog: None,
            new_folder: None,
            show_settings: false,
            show_quit: false,
        }
    }

    fn message(&mut self, text: String) {
        self.messages.push(format!("{}  {}", Job::now(), text));
    }

    fn start_session(&mut self) {
        if self.running.load(Ordering::Relaxed) {
            return;
        }
        self.stop.store(false, Ordering::Relaxed);
        self.pause.store(false, Ordering::Relaxed);
        self.stop_after_frame.store(false, Ordering::Relaxed);
        self.live.lock().unwrap().lines.clear();
        self.running.store(true, Ordering::Relaxed);
        let queue = self.queue.clone();
        let stop = self.stop.clone();
        let pause = self.pause.clone();
        let stop_after = self.stop_after_frame.clone();
        let live = self.live.clone();
        let running = self.running.clone();
        let cfg = RunConfig {
            workers: self.defaults.workers.max(1),
            blender: self.defaults.blender.clone(),
            stop_after_job: false,
            stop_after_frame: stop_after,
        };
        self.session = Some(std::thread::spawn(move || {
            let _ = run_session(queue, &cfg, &stop, &pause, Some(live));
            running.store(false, Ordering::Relaxed);
        }));
        self.message("session started".to_string());
    }

    fn stop_now(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.pause.store(false, Ordering::Relaxed);
        self.message("stopping - killing Blender".to_string());
    }

    fn selected_job(&self) -> Option<Job> {
        let name = self.selected.clone()?;
        self.queue.lock().unwrap().load_job(&name)
    }
}

impl eframe::App for BbrApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let running = self.running.load(Ordering::Relaxed);

        // ---- dropped .blend files ----
        let dropped: Vec<PathBuf> = ctx.input(|i| {
            i.raw
                .dropped_files
                .iter()
                .filter_map(|d| d.path.clone())
                .filter(|p| p.extension().map(|e| e == "blend").unwrap_or(false))
                .collect()
        });
        if !dropped.is_empty() {
            let mut dlg = dialogs::AddJobs::new(self.defaults.settings.clone());
            dlg.files = dropped.iter().map(|p| p.display().to_string()).collect();
            self.add_dialog = Some(dlg);
        }

        // ---- toolbar ----
        egui::TopBottomPanel::top("toolbar").show(ctx, |ui| {
            ui.horizontal_wrapped(|ui| {
                if ui.button("Add .blend…").clicked() {
                    self.add_dialog = Some(dialogs::AddJobs::new(self.defaults.settings.clone()));
                }
                if ui.button("New folder…").clicked() {
                    self.new_folder = Some(String::new());
                }
                let have_sel = self.selected.is_some();
                let mut remove_clicked = false;
                let mut up_clicked = false;
                let mut down_clicked = false;
                if have_sel {
                    remove_clicked = ui.button("Remove…").clicked();
                    up_clicked = ui.button("▲").clicked();
                    down_clicked = ui.button("▼").clicked();
                } else {
                    ui.add_enabled(false, egui::Button::new("Remove…"));
                    ui.add_enabled(false, egui::Button::new("▲"));
                    ui.add_enabled(false, egui::Button::new("▼"));
                }
                ui.separator();
                if ui
                    .add_enabled(!running, egui::Button::new("▶  Start"))
                    .on_hover_text("Render the queue (resumes where it stopped)")
                    .clicked()
                {
                    self.start_session();
                }
                if ui
                    .add_enabled(
                        running,
                        egui::Button::new(if self.stop_after_frame.load(Ordering::Relaxed) {
                            "Stop after frame ✓"
                        } else {
                            "Stop after frame"
                        }),
                    )
                    .on_hover_text("Let the current frame finish, then stop")
                    .clicked()
                {
                    let v = !self.stop_after_frame.load(Ordering::Relaxed);
                    self.stop_after_frame.store(v, Ordering::Relaxed);
                }
                if ui
                    .add_enabled(running, egui::Button::new("■  Stop now"))
                    .on_hover_text(
                        "Kill Blender now - the current frame is rendered again next time",
                    )
                    .clicked()
                {
                    self.stop_now();
                }
                let paused = self.pause.load(Ordering::Relaxed);
                if ui
                    .add_enabled(
                        running,
                        egui::Button::new(if paused { "Resume" } else { "Pause" }),
                    )
                    .clicked()
                {
                    self.pause.store(!paused, Ordering::Relaxed);
                }
                ui.separator();
                ui.add(egui::Slider::new(&mut self.defaults.workers, 1..=16).text("workers"));
                if ui.button("Settings…").clicked() {
                    self.show_settings = true;
                }
                if ui.button("Quit").clicked() {
                    if running {
                        self.show_quit = true;
                    } else {
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                }
                if remove_clicked {
                    if let Some(name) = self.selected.clone() {
                        let _ = self.queue.lock().unwrap().remove(&name);
                        self.message(format!("{} removed", name));
                        self.selected = None;
                        self.draft = None;
                    }
                }
                if up_clicked {
                    if let Some(name) = self.selected.clone() {
                        let _ = self.queue.lock().unwrap().move_job(&name, true);
                    }
                }
                if down_clicked {
                    if let Some(name) = self.selected.clone() {
                        let _ = self.queue.lock().unwrap().move_job(&name, false);
                    }
                }
            });
        });

        // ---- details panel (right) ----
        egui::SidePanel::right("details")
            .resizable(true)
            .default_width(420.0)
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.tab, DetailsTab::Live, "Live");
                    ui.selectable_value(&mut self.tab, DetailsTab::Settings, "Job settings");
                });
                ui.separator();
                let sel = self.selected_job();
                match self.tab {
                    DetailsTab::Live => {
                        let live = self.live.lock().unwrap().clone();
                        live::live_tab(ui, sel.as_ref(), &live);
                    }
                    DetailsTab::Settings => {
                        let Some(job) = sel.as_ref() else {
                            ui.label("Select a job in the queue to edit its settings.");
                            return;
                        };
                        // keep a persistent draft per job selection
                        let needs_reset = self
                            .draft
                            .as_ref()
                            .map(|(n, _)| n != &job.name)
                            .unwrap_or(true);
                        if needs_reset {
                            self.draft = Some((job.name.clone(), job.settings.clone()));
                        }
                        let mut apply = false;
                        let mut set_defaults = false;
                        let installs = crate::installs::load_installs();
                        if let Some((_, draft)) = self.draft.as_mut() {
                            job_form::job_settings_form(ui, draft, &installs);
                            ui.add_space(6.0);
                            let lock = running || job.status == Status::Rendering;
                            ui.horizontal(|ui| {
                                if ui
                                    .add_enabled(!lock, egui::Button::new("Apply to job"))
                                    .on_hover_text(if lock {
                                        "This job is rendering - stop it to change settings."
                                    } else {
                                        "Save these settings for the selected job. They apply the next time it renders."
                                    })
                                    .clicked()
                                {
                                    apply = true;
                                }
                                if ui.button("Set as defaults").clicked() {
                                    set_defaults = true;
                                }
                            });
                        }
                        if apply {
                            let draft = self.draft.as_ref().unwrap().1.clone();
                            match self.apply_settings(&job.name, &draft) {
                                Ok(()) => self.message(format!("settings saved for {}", job.name)),
                                Err(e) => self.message(format!("settings: {}", e)),
                            }
                        }
                        if set_defaults {
                            self.defaults.settings = self.draft.as_ref().unwrap().1.clone();
                            save_defaults(&self.defaults);
                            self.message("defaults saved".to_string());
                        }
                    }
                }
            });

        // ---- console / messages (bottom) ----
        egui::TopBottomPanel::bottom("bottom")
            .resizable(true)
            .default_height(180.0)
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.bottom, BottomTab::Console, "Console");
                    ui.selectable_value(&mut self.bottom, BottomTab::Messages, "Messages");
                    if self.bottom == BottomTab::Console {
                        ui.separator();
                        ui.checkbox(&mut self.follow, "Follow rendering").on_hover_text(
                            "Show the console of the job that is rendering, instead of the selected one",
                        );
                        if ui.button("Open logs folder").clicked() {
                            if let Some(job) = self.selected_job() {
                                open_path(&job.logs_dir());
                            }
                        }
                    }
                });
                ui.separator();
                match self.bottom {
                    BottomTab::Console => {
                        let follow = self.follow && running;
                        egui::ScrollArea::vertical()
                            .stick_to_bottom(follow)
                            .show(ui, |ui| {
                                let live = self.live.lock().unwrap();
                                for line in &live.lines {
                                    ui.monospace(line);
                                }
                            });
                    }
                    BottomTab::Messages => {
                        egui::ScrollArea::vertical()
                            .stick_to_bottom(true)
                            .show(ui, |ui| {
                                for m in &self.messages {
                                    ui.monospace(m);
                                }
                            });
                    }
                }
            });

        // ---- queue tree (centre) ----
        egui::CentralPanel::default().show(ctx, |ui| {
            let action = {
                let mut q = self.queue.lock().unwrap();
                queue::queue_panel(ui, &mut q, &mut self.selected, running)
            };
            match action {
                queue::QueueAction::None => {}
                queue::QueueAction::Requeue { name, keep } => {
                    {
                        let mut q = self.queue.lock().unwrap();
                        let _ = q.requeue(&name, keep);
                    }
                    self.message(format!(
                        "{} queued again ({})",
                        name,
                        if keep { "only failed frames" } else { "every frame" }
                    ));
                }
                queue::QueueAction::AcceptChanged(name) => {
                    let accepted = {
                        let mut q = self.queue.lock().unwrap();
                        q.accept_changed_blend(&name)
                    };
                    if accepted {
                        self.message(format!("{} continues with the changed .blend", name));
                    }
                }
                queue::QueueAction::OpenFolder(name) => {
                    if let Some(job) = self.queue.lock().unwrap().load_job(&name) {
                        open_path(Path::new(&job.folder));
                    }
                }
                queue::QueueAction::OpenReport(name) => {
                    if let Some(job) = self.queue.lock().unwrap().load_job(&name) {
                        open_path(&job.data_dir().join("report.xlsx"));
                    }
                }
                queue::QueueAction::MoveToFolder { name, folder } => {
                    let mut q = self.queue.lock().unwrap();
                    let _ = q.move_to_folder(&name, folder.as_deref());
                }
            }
        });

        // ---- add-jobs dialog ----
        if self.add_dialog.is_some() {
            let mut result = None; // Some(true)=add, Some(false)=cancel
            let folders: Vec<String> = {
                let q = self.queue.lock().unwrap();
                q.folders.iter().map(|f| f.name.clone()).collect()
            };
            {
                let dlg = self.add_dialog.as_mut().unwrap();
                let mut open = true;
                let mut add = false;
                let mut cancel = false;
                egui::Window::new("Add .blend files")
                    .open(&mut open)
                    .default_width(520.0)
                    .show(ctx, |ui| {
                        if ui.button("Choose files…").clicked() {
                            if let Some(files) = rfd::FileDialog::new()
                                .add_filter("Blender", &["blend"])
                                .pick_files()
                            {
                                for f in files {
                                    let p = f.display().to_string();
                                    if !dlg.files.contains(&p) {
                                        dlg.files.push(p);
                                    }
                                }
                            }
                        }
                        ui.add_space(4.0);
                        egui::ScrollArea::vertical().max_height(160.0).show(ui, |ui| {
                            let mut rm: Option<usize> = None;
                            for (i, f) in dlg.files.iter().enumerate() {
                                ui.horizontal(|ui| {
                                    ui.monospace(f);
                                    if ui.small_button("✕").clicked() {
                                        rm = Some(i);
                                    }
                                });
                            }
                            if let Some(i) = rm {
                                dlg.files.remove(i);
                            }
                        });
                        ui.add_space(4.0);
                        ui.horizontal(|ui| {
                            ui.label("Folder:");
                            egui::ComboBox::from_id_salt("add_folder")
                                .selected_text(
                                    dlg.folder
                                        .clone()
                                        .unwrap_or_else(|| "(top level)".to_string()),
                                )
                                .show_ui(ui, |ui| {
                                    ui.selectable_value(&mut dlg.folder, None, "(top level)");
                                    for f in &folders {
                                        let fv = Some(f.clone());
                                        ui.selectable_value(&mut dlg.folder, fv, f);
                                    }
                                });
                        });
                        ui.separator();
                        job_form::job_settings_form(ui, &mut dlg.settings, &[]);
                        ui.add_space(6.0);
                        ui.horizontal(|ui| {
                            if ui.button("Cancel").clicked() {
                                cancel = true;
                            }
                            if ui
                                .add_enabled(
                                    !dlg.files.is_empty(),
                                    egui::Button::new("Add to queue"),
                                )
                                .clicked()
                            {
                                add = true;
                            }
                        });
                    });
                if add || cancel {
                    open = false;
                }
                if !open {
                    result = Some(add);
                }
            }
            if let Some(add) = result {
                if add {
                    // copy what we need out of the dialog first so no borrow
                    // of self is held while the queue is locked
                    let (files, folder, settings) = {
                        let dlg = self.add_dialog.as_ref().unwrap();
                        (dlg.files.clone(), dlg.folder.clone(), dlg.settings.clone())
                    };
                    let mut ok = 0;
                    {
                        let mut q = self.queue.lock().unwrap();
                        for f in &files {
                            if q
                                .add(Path::new(f), settings.clone(), folder.as_deref())
                                .is_ok()
                            {
                                ok += 1;
                            }
                        }
                    }
                    self.message(format!("{} job(s) added", ok));
                }
                self.add_dialog = None;
            }
        }

        // ---- new folder dialog ----
        if self.new_folder.is_some() {
            // keep the text in a local so we do not hold a borrow of self
            let mut buffer = self.new_folder.clone().unwrap();
            let mut open = true;
            let mut create = false;
            let mut cancel = false;
            egui::Window::new("New folder")
                .open(&mut open)
                .resizable(false)
                .show(ctx, |ui| {
                    ui.text_edit_singleline(&mut buffer);
                    ui.horizontal(|ui| {
                        if ui.button("Cancel").clicked() {
                            cancel = true;
                        }
                        if ui
                            .add_enabled(!buffer.trim().is_empty(), egui::Button::new("Create"))
                            .clicked()
                        {
                            create = true;
                        }
                    });
                });
            if !open || create || cancel {
                if create {
                    let name = buffer.trim().to_string();
                    let _ = self.queue.lock().unwrap().new_folder(&name);
                    self.message(format!("folder '{}' created", name));
                }
                self.new_folder = None;
            }
        }

        // ---- app settings dialog ----
        if self.show_settings {
            let mut open = self.show_settings;
            dialogs::settings_dialog(ctx, &mut open, &mut self.defaults);
            if !open {
                save_defaults(&self.defaults);
                self.show_settings = false;
            }
        }

        // ---- quit dialog ----
        if self.show_quit && running {
            let mut open = self.show_quit;
            // the closure must not touch open (egui borrows it), so collect
            // the outcome in flags and apply them after the window is drawn
            let mut quit_now = false;
            let mut quit_after_frame = false;
            let mut keep = false;
            egui::Window::new("Quit?")
                .open(&mut open)
                .resizable(false)
                .show(ctx, |ui| {
                    ui.label("Blender is still rendering.");
                    if ui.button("Quit now").clicked() {
                        quit_now = true;
                    }
                    if ui.button("Finish this frame, then quit").clicked() {
                        quit_after_frame = true;
                    }
                    if ui.button("Keep rendering").clicked() {
                        keep = true;
                    }
                });
            if quit_now {
                self.stop_now();
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            } else if quit_after_frame {
                self.stop_after_frame.store(true, Ordering::Relaxed);
                self.message("will quit after the current frame".to_string());
            }
            if !open || quit_now || quit_after_frame || keep {
                self.show_quit = false;
            }
        }

        // keep the window fresh while rendering, calm otherwise
        if running {
            ctx.request_repaint_after(std::time::Duration::from_millis(500));
        }
    }
}

impl BbrApp {
    /// Apply edited settings to a job (writes job.json).
    fn apply_settings(&mut self, name: &str, settings: &JobSettings) -> anyhow::Result<()> {
        let q = self.queue.lock().unwrap();
        let mut job = q
            .load_job(name)
            .ok_or_else(|| anyhow::anyhow!("no job named {}", name))?;
        if job.status == Status::Rendering {
            anyhow::bail!("this job is rendering - stop it to change settings");
        }
        job.settings = settings.clone();
        job.save()?;
        Ok(())
    }
}
