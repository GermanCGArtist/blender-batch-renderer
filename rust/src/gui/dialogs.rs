//! Dialogs: the add-jobs dialog state and the app settings dialog — ported
//! from dialogs.py (SettingsDialog / AddJobsDialog).

use super::AppDefaults;
use crate::installs;
use crate::models::JobSettings;
use eframe::egui;

pub struct AddJobs {
    pub files: Vec<String>,
    pub folder: Option<String>,
    pub settings: JobSettings,
}

impl AddJobs {
    pub fn new(settings: JobSettings) -> Self {
        AddJobs {
            files: Vec::new(),
            folder: None,
            settings,
        }
    }
}

/// The app settings dialog: Blender installs, defaults for new jobs, workers.
/// Saved by the caller when the dialog closes.
pub fn settings_dialog(ctx: &egui::Context, open: &mut bool, defaults: &mut AppDefaults) {
    egui::Window::new("Settings")
        .open(open)
        .default_width(460.0)
        .show(ctx, |ui| {
            // ---- Blender installs ----
            ui.heading("Blender installs");
            let mut found = installs::load_installs();
            if found.is_empty() {
                ui.label("None found yet - scan to look for Blender.");
            }
            for i in &found {
                ui.label(format!("{}   (Blender {})", i.path, i.version));
            }
            ui.horizontal(|ui| {
                if ui.button("Scan").clicked() {
                    found = installs::scan();
                }
                if ui.button("Add manually…").clicked() {
                    if let Some(p) = rfd::FileDialog::new().pick_file() {
                        if let Some(inst) = installs::probe(&p) {
                            if !found.iter().any(|i| i.path == inst.path) {
                                found.push(inst);
                            }
                            let _ = installs::save_installs(&found);
                        }
                    }
                }
            });
            ui.separator();

            // ---- Defaults for new jobs ----
            ui.heading("Defaults for new jobs");
            job_form(ui, &mut defaults.settings, &found);
            ui.separator();

            // ---- App ----
            ui.heading("App");
            egui::Grid::new("app_settings")
                .num_columns(2)
                .spacing([10.0, 5.0])
                .show(ui, |ui| {
                    ui.label("Workers");
                    ui.add(egui::Slider::new(&mut defaults.workers, 1..=16));
                    ui.end_row();
                    ui.label("Blender");
                    egui::ComboBox::from_id_salt("def_blender")
                        .selected_text(if defaults.blender == "auto" {
                            "auto (newest)".to_string()
                        } else {
                            defaults.blender.clone()
                        })
                        .show_ui(ui, |ui| {
                            ui.selectable_value(
                                &mut defaults.blender,
                                "auto".to_string(),
                                "auto (newest)",
                            );
                            for i in &found {
                                ui.selectable_value(
                                    &mut defaults.blender,
                                    i.path.clone(),
                                    format!("Blender {}", i.version),
                                );
                            }
                        });
                    ui.end_row();
                });
            ui.add_space(6.0);
            ui.label(
                egui::RichText::new("Settings are saved when the dialog closes.")
                    .small()
                    .weak(),
            );
        });
}

use crate::gui::job_form::job_settings_form as job_form;
