//! The job settings form — ported from widgets.py JobSettingsForm.
//!
//! Optional overrides ("unticked = as saved in the .blend") are represented by
//! a checkbox + value; in the Rust core 0 means "keep the value from the file".

use crate::installs::Install;
use crate::models::JobSettings;
use eframe::egui;

fn section(ui: &mut egui::Ui, title: &str) {
    ui.label(egui::RichText::new(title).strong());
}

pub fn job_settings_form(ui: &mut egui::Ui, s: &mut JobSettings, installs: &[Install]) {
    // work on locals so egui widgets never borrow s directly
    let mut blender = s.blender.clone();
    let mut start = s.start;
    let mut end = s.end;
    let mut step = s.step;
    let mut use_samples = s.samples > 0;
    let mut samples = if use_samples { s.samples as i32 } else { 128 };
    let mut use_pct = s.pct > 0 && s.pct != 100;
    let mut pct = if use_pct { s.pct as i32 } else { 100 };
    let mut engine = s.engine.clone();
    let mut format = s.format.clone();
    let mut attempts = s.attempts as i32;
    let mut delay = s.delay_s as i32;
    let mut timeout = s.timeout_min as i32;

    egui::Grid::new("job_settings")
        .num_columns(2)
        .spacing([10.0, 5.0])
        .show(ui, |ui| {
            ui.label("Blender").on_hover_text(
                "Which Blender renders the job. Auto: the newest install found.",
            );
            egui::ComboBox::from_id_salt("form_blender")
                .selected_text(if blender == "auto" {
                    "Auto (newest)".to_string()
                } else {
                    blender.clone()
                })
                .show_ui(ui, |ui| {
                    ui.selectable_value(&mut blender, "auto".to_string(), "Auto (newest)");
                    for i in installs {
                        ui.selectable_value(
                            &mut blender,
                            i.path.clone(),
                            format!("{} (Blender {})", i.path, i.version),
                        );
                    }
                });
            ui.end_row();

            section(ui, "Frames");
            ui.end_row();
            ui.label("Start frame");
            ui.add(egui::DragValue::new(&mut start));
            ui.end_row();
            ui.label("End frame");
            ui.add(egui::DragValue::new(&mut end));
            ui.end_row();
            ui.label("Frame step");
            ui.add(egui::DragValue::new(&mut step).range(1..=1000));
            ui.end_row();

            section(ui, "Overrides (unticked = as saved in the file)");
            ui.end_row();
            ui.label("Samples").on_hover_text("Render samples (Cycles or EEVEE)");
            ui.horizontal(|ui| {
                ui.checkbox(&mut use_samples, "");
                ui.add_enabled(
                    use_samples,
                    egui::DragValue::new(&mut samples).range(1..=1_000_000),
                );
            });
            ui.end_row();
            ui.label("Resolution %");
            ui.horizontal(|ui| {
                ui.checkbox(&mut use_pct, "");
                ui.add_enabled(use_pct, egui::DragValue::new(&mut pct).range(1..=1000));
            });
            ui.end_row();

            ui.label("Engine");
            egui::ComboBox::from_id_salt("form_engine")
                .selected_text(engine.clone())
                .show_ui(ui, |ui| {
                    for e in ["auto", "CYCLES", "BLENDER_EEVEE_NEXT"] {
                        ui.selectable_value(&mut engine, e.to_string(), e);
                    }
                });
            ui.end_row();

            ui.label("Format");
            egui::ComboBox::from_id_salt("form_format")
                .selected_text(format.clone())
                .show_ui(ui, |ui| {
                    for f in ["PNG", "OPEN_EXR", "JPEG", "FFMPEG"] {
                        ui.selectable_value(&mut format, f.to_string(), f);
                    }
                });
            ui.end_row();

            section(ui, "When something goes wrong");
            ui.end_row();
            ui.label("Attempts").on_hover_text(
                "How often a frame is tried before it is marked as failed and skipped.",
            );
            ui.add(egui::DragValue::new(&mut attempts).range(1..=100));
            ui.end_row();
            ui.label("Retry delay (s)");
            ui.add(egui::DragValue::new(&mut delay).range(0..=86_400));
            ui.end_row();
            ui.label("Timeout (min)").on_hover_text(
                "A run that takes longer than this is stopped and counts as a failed attempt. 0: off.",
            );
            ui.add(egui::DragValue::new(&mut timeout).range(0..=10_080));
            ui.end_row();
        });

    // write back
    s.blender = blender;
    s.start = start;
    s.end = end;
    s.step = step.max(1);
    s.samples = if use_samples { (samples as u32).max(1) } else { 0 };
    s.pct = if use_pct { (pct as u32).max(1) } else { 0 };
    s.engine = engine;
    s.format = format;
    s.attempts = (attempts as u32).max(1);
    s.delay_s = (delay as u64).max(0);
    s.timeout_min = (timeout as u64).max(0);
}
