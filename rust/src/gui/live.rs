//! The Live tab: current frame, job progress and statistics — ported from
//! main_window.py's _build_live_tab / _refresh_live.

use crate::models::Job;
use crate::runner::{fmt_duration, LiveState};
use eframe::egui;

fn stat(ui: &mut egui::Ui, label: &str, value: &str, tip: &str) {
    ui.label(egui::RichText::new(label).weak()).on_hover_text(tip);
    ui.label(value).on_hover_text(tip);
    ui.end_row();
}

pub fn live_tab(ui: &mut egui::Ui, job: Option<&Job>, live: &LiveState) {
    // ---- current frame ----
    ui.group(|ui| {
        ui.heading("Current frame");
        if live.cur_job.is_empty() {
            ui.label("–  (idle)");
        } else {
            ui.label(
                egui::RichText::new(format!("Frame {}  ·  {}", live.cur_frame, live.cur_job))
                    .size(18.0)
                    .strong(),
            );
            ui.add(
                egui::ProgressBar::new(live.cur_pct.clamp(0.0, 100.0) / 100.0)
                    .text(format!("{:.0}%", live.cur_pct)),
            );
        }
    });
    ui.add_space(8.0);

    // ---- selected job ----
    let Some(job) = job else {
        ui.label("Select a job in the queue to see its details.");
        return;
    };
    ui.heading(&job.name);

    let total = job.frame_numbers().len();
    let done = job.frames_done();
    let missing = job.frames_missing().len();
    ui.add(egui::ProgressBar::new(if total > 0 {
        done as f32 / total as f32
    } else {
        0.0
    }));
    ui.add_space(4.0);

    // ---- job statistics ----
    let secs: Vec<f64> = job
        .frames
        .values()
        .filter(|r| r.status == "done")
        .filter_map(|r| r.seconds)
        .collect();
    let avg = if secs.is_empty() {
        None
    } else {
        Some(secs.iter().sum::<f64>() / secs.len() as f64)
    };
    let render_time: f64 = secs.iter().sum();
    let eta = avg.map(|a| a * missing as f64).filter(|e| *e > 0.0);
    let done_at = eta.map(|e| {
        (chrono::Local::now() + chrono::Duration::seconds(e as i64))
            .format("%H:%M")
            .to_string()
    });

    egui::Grid::new("job_stats")
        .num_columns(2)
        .spacing([10.0, 4.0])
        .show(ui, |ui| {
            stat(ui, "Frames", &format!("{} done · {} failed/missing · {} total", done, missing, total), "Finished, failed and remaining frames");
            stat(
                ui,
                "Avg frame",
                &avg.map(|a| fmt_duration(a)).unwrap_or_else(|| "–".into()),
                "Average render time of the finished frames",
            );
            stat(
                ui,
                "Remaining",
                &eta.map(fmt_duration).unwrap_or_else(|| "–".into()),
                "Estimated time until the job is done",
            );
            stat(
                ui,
                "Done at",
                &done_at.unwrap_or_else(|| "–".into()),
                "Estimated clock time when the job is finished",
            );
            stat(
                ui,
                "Render time",
                &fmt_duration(render_time),
                "Render time of all finished frames added up",
            );
            stat(
                ui,
                "Blender runs",
                &job.attempts.len().to_string(),
                "How often Blender was started for this job (retries)",
            );
            stat(
                ui,
                "Engine",
                &format!("{} · {}", job.settings.engine, job.settings.format),
                "Render engine and output format",
            );
            stat(
                ui,
                "Status",
                job.status.as_str(),
                job.status_note.as_str(),
            );
        });

    // ---- frame times chart ----
    if !secs.is_empty() {
        ui.add_space(8.0);
        ui.label(egui::RichText::new("Frame render times").strong());
        let height = 120.0;
        let (rect, _) = ui.allocate_exact_size(
            egui::vec2(ui.available_width(), height),
            egui::Sense::hover(),
        );
        let painter = ui.painter_at(rect);
        painter.rect_stroke(rect, 2.0, egui::Stroke::new(1.0_f32, egui::Color32::GRAY));
        let max = secs.iter().cloned().fold(0.0_f64, f64::max).max(0.001);
        let n = secs.len() as f64;
        let mut prev: Option<egui::Pos2> = None;
        for (i, y) in secs.iter().enumerate() {
            let x = rect.left() + (i as f32 + 0.5) / n as f32 * rect.width();
            let yy = rect.bottom() - (*y as f32 / max as f32) * (rect.height() - 8.0) - 4.0;
            let p = egui::pos2(x, yy);
            painter.circle_filled(p, 2.0, egui::Color32::from_rgb(0x3d, 0x8b, 0xfd));
            if let Some(pp) = prev {
                painter.line_segment([pp, p], egui::Stroke::new(2.0_f32, egui::Color32::from_rgb(0x3d, 0x8b, 0xfd)));
            }
            prev = Some(p);
        }
        ui.monospace(format!("peak {} · avg {:.1}s", fmt_duration(max), secs.iter().sum::<f64>() / n));
    }
}
