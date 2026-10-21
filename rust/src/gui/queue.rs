//! The queue as a tree: folders containing jobs, with enable checkboxes,
//! selection and per-job actions — ported from queue_tree.py / main_window.py.

use crate::jobqueue::JobQueue;
use crate::models::{Job, Status};
use eframe::egui;

pub enum QueueAction {
    None,
    Requeue { name: String, keep: bool },
    AcceptChanged(String),
    OpenFolder(String),
    OpenReport(String),
    MoveToFolder { name: String, folder: Option<String> },
}

/// Columns: enable | job | status | progress | frames | ETA
const COLS: [&str; 6] = ["", "Job", "Status", "Progress", "Frames", "ETA"];

fn eta_text(job: &Job) -> String {
    let done: Vec<f64> = job
        .frames
        .values()
        .filter(|r| r.status == "done")
        .filter_map(|r| r.seconds)
        .collect();
    let total = job.frame_numbers().len() as f64;
    let missing = job.frames_missing().len() as f64;
    if total <= 0.0 {
        return String::new();
    }
    if missing == 0.0 {
        return "done".to_string();
    }
    if done.is_empty() {
        return String::new();
    }
    let avg: f64 = done.iter().sum::<f64>() / done.len() as f64;
    crate::runner::fmt_duration(avg * missing)
}

fn status_color(status: &Status) -> egui::Color32 {
    match status {
        Status::Queued => egui::Color32::GRAY,
        Status::Rendering => egui::Color32::from_rgb(0x3d, 0x8b, 0xfd),
        Status::Done => egui::Color32::from_rgb(0x2f, 0xa8, 0x4f),
        Status::DoneErrors => egui::Color32::from_rgb(0xe6, 0x7e, 0x22),
        Status::Failed => egui::Color32::from_rgb(0xd9, 0x53, 0x4f),
        Status::Stopped | Status::Cancelled | Status::OnHold => egui::Color32::YELLOW,
    }
}

fn header_row(ui: &mut egui::Ui, id: &str) {
    egui::Grid::new(id)
        .num_columns(COLS.len())
        .spacing([10.0, 4.0])
        .show(ui, |ui| {
            for c in COLS {
                ui.strong(c);
            }
            ui.end_row();
        });
}

/// Draw one job as a row of the grid the caller opened.
fn job_cells(
    ui: &mut egui::Ui,
    q: &mut JobQueue,
    job: &Job,
    selected: &mut Option<String>,
    running: bool,
) {
    let is_sel = selected.as_deref() == Some(job.name.as_str());
    let mut enabled = job.enabled;
    let total = job.frame_numbers().len().max(1);
    let pct = job.frames_done() as f32 / total as f32;
    let missing = job.frames_missing().len();
    let frames = format!(
        "{}/{}{}",
        job.frames_done(),
        total,
        if missing > 0 {
            format!(" −{}", missing)
        } else {
            String::new()
        }
    );

    if running && job.status == Status::Rendering {
        ui.add_enabled(false, egui::Checkbox::new(&mut enabled, ""));
    } else if ui.checkbox(&mut enabled, "").changed() {
        let _ = q.set_enabled(&job.name, enabled);
    }
    if ui.selectable_label(is_sel, &job.name).clicked() {
        *selected = Some(job.name.clone());
    }
    ui.colored_label(status_color(&job.status), job.status.as_str());
    ui.add_sized([90.0, 14.0], egui::ProgressBar::new(pct).show_percentage());
    ui.label(frames);
    ui.label(eta_text(job));
    ui.end_row();
}

pub fn queue_panel(
    ui: &mut egui::Ui,
    q: &mut JobQueue,
    selected: &mut Option<String>,
    running: bool,
) -> QueueAction {
    let mut action = QueueAction::None;

    egui::ScrollArea::vertical().show(ui, |ui| {
        // top-level jobs
        header_row(ui, "head");
        ui.separator();
        let jobs: Vec<Job> = q.load_jobs();
        let top: Vec<Job> = jobs
            .iter()
            .filter(|j| !q.folders.iter().any(|f| f.jobs.contains(&j.folder)))
            .cloned()
            .collect();
        let grid = egui::Grid::new("top_jobs")
            .num_columns(COLS.len())
            .spacing([10.0, 4.0])
            .striped(true);
        grid.show(ui, |ui| {
            for job in &top {
                job_cells(ui, q, job, selected, running);
            }
        });

        // folders with their jobs
        let folders: Vec<(String, Vec<String>)> = q
            .folders
            .iter()
            .map(|f| (f.name.clone(), f.jobs.clone()))
            .collect();
        for (fname, paths) in folders {
            ui.add_space(4.0);
            egui::CollapsingHeader::new(format!("📁 {}", fname))
                .default_open(true)
                .id_salt(format!("folder_{}", fname))
                .show(ui, |ui| {
                    let all: Vec<Job> = q.load_jobs();
                    let inner: Vec<Job> = all
                        .iter()
                        .filter(|j| paths.contains(&j.folder))
                        .cloned()
                        .collect();
                    let n = inner.len().to_string();
                    egui::Grid::new(format!("grid_{}", fname))
                        .num_columns(COLS.len())
                        .spacing([10.0, 4.0])
                        .striped(true)
                        .show(ui, |ui| {
                            let _ = &n;
                            for job in &inner {
                                job_cells(ui, q, job, selected, running);
                            }
                        });
                });
        }
    });

    ui.separator();

    // ---- per-job actions for the selected job ----
    if let Some(name) = selected.clone() {
        let job = q.load_job(&name);
        let rendering = job
            .as_ref()
            .map(|j| j.status == Status::Rendering)
            .unwrap_or(false);
        egui::Grid::new("actions")
            .num_columns(7)
            .spacing([6.0, 2.0])
            .show(ui, |ui| {
                if ui
                    .add_enabled(!rendering, egui::Button::new("Retry failed frames"))
                    .on_hover_text("Queue the job again, rendering only frames that failed")
                    .clicked()
                {
                    action = QueueAction::Requeue { name: name.clone(), keep: true };
                }
                if ui
                    .add_enabled(!rendering, egui::Button::new("Start job over…"))
                    .on_hover_text("Forget progress and render every frame again")
                    .clicked()
                {
                    action = QueueAction::Requeue { name: name.clone(), keep: false };
                }
                let changed = job
                    .as_ref()
                    .map(|j| j.status == Status::OnHold && j.blend_changed())
                    .unwrap_or(false);
                if ui
                    .add_enabled(changed, egui::Button::new("Continue with changed file"))
                    .on_hover_text(
                        "The .blend was saved again after frames were rendered: keep the finished frames and render the rest from the changed file",
                    )
                    .clicked()
                {
                    action = QueueAction::AcceptChanged(name.clone());
                }
                if ui.button("Open render folder").clicked() {
                    action = QueueAction::OpenFolder(name.clone());
                }
                if ui.button("Open report").clicked() {
                    action = QueueAction::OpenReport(name.clone());
                }
                // move to folder
                let current = q
                    .folders
                    .iter()
                    .find(|f| job.as_ref().map(|j| f.jobs.contains(&j.folder)).unwrap_or(false))
                    .map(|f| f.name.clone());
                egui::ComboBox::from_id_salt(format!("mv_{}", name))
                    .selected_text(current.clone().unwrap_or_else(|| "(top level)".to_string()))
                    .show_ui(ui, |ui| {
                        if ui
                            .selectable_label(current.is_none(), "(top level)")
                            .clicked()
                        {
                            action = QueueAction::MoveToFolder { name: name.clone(), folder: None };
                        }
                        for f in &q.folders {
                            if ui
                                .selectable_label(current.as_deref() == Some(f.name.as_str()), &f.name)
                                .clicked()
                            {
                                action = QueueAction::MoveToFolder {
                                    name: name.clone(),
                                    folder: Some(f.name.clone()),
                                };
                            }
                        }
                    });
                ui.end_row();
            });
        if running {
            ui.label(
                egui::RichText::new("⏳ rendering - job settings are read-only while a job renders")
                    .small(),
            );
        }
    } else {
        ui.label("Select a job to see its details and actions.");
    }
    action
}
