//! CSV and XLSX reports per job: frames.csv, attempts.csv, report.xlsx.

use crate::models::Job;
use anyhow::Result;
use std::fs;
use std::path::Path;

pub fn frames_csv(job: &Job) -> Result<std::path::PathBuf> {
    let path = job.data_dir().join("frames.csv");
    let mut wtr = csv::Writer::from_path(&path)?;
    wtr.write_record(["frame", "status", "attempts", "seconds"])?;
    for f in job.frame_numbers() {
        let r = job.frames.get(&f);
        wtr.write_record([
            f.to_string(),
            r.map(|r| r.status.clone()).unwrap_or_else(|| "pending".into()),
            r.map(|r| r.attempts.to_string()).unwrap_or_else(|| "0".into()),
            r.and_then(|r| r.seconds)
                .map(|s| format!("{:.1}", s))
                .unwrap_or_default(),
        ])?;
    }
    wtr.flush()?;
    Ok(path)
}

pub fn attempts_csv(job: &Job) -> Result<std::path::PathBuf> {
    let path = job.data_dir().join("attempts.csv");
    let mut wtr = csv::Writer::from_path(&path)?;
    wtr.write_record(["attempt", "started", "ended", "ok", "exit_code", "frames_done", "frames_failed", "log"])?;
    for a in &job.attempts {
        wtr.write_record([
            a.index.to_string(),
            a.started.clone(),
            a.ended.clone(),
            a.ok.to_string(),
            a.exit_code
                .map(|c| c.to_string())
                .unwrap_or_else(|| "-".into()),
            a.frames_done.to_string(),
            a.frames_failed.to_string(),
            a.log.clone(),
        ])?;
    }
    wtr.flush()?;
    Ok(path)
}

pub fn xlsx(job: &Job) -> Result<std::path::PathBuf> {
    let mut workbook = rust_xlsxwriter::Workbook::new();

    let mut sheet = workbook.add_worksheet().set_name("frames")?;
    sheet.write_string(0, 0, "frame")?;
    sheet.write_string(0, 1, "status")?;
    sheet.write_string(0, 2, "attempts")?;
    sheet.write_string(0, 3, "seconds")?;
    for (i, f) in job.frame_numbers().into_iter().enumerate() {
        let r = job.frames.get(&f);
        sheet.write_number((i + 1) as u32, 0, f as f64)?;
        sheet.write_string(
            (i + 1) as u32,
            1,
            r.map(|r| r.status.clone())
                .unwrap_or_else(|| "pending".into())
                .as_str(),
        )?;
        sheet.write_number(
            (i + 1) as u32,
            2,
            r.map(|r| r.attempts).unwrap_or(0) as f64,
        )?;
        if let Some(s) = r.and_then(|r| r.seconds) {
            sheet.write_number((i + 1) as u32, 3, s)?;
        }
    }

    let mut sheet = workbook.add_worksheet().set_name("attempts")?;
    let headers = ["attempt", "started", "ended", "ok", "exit_code", "frames_done", "frames_failed", "log"];
    for (c, h) in headers.iter().enumerate() {
        sheet.write_string(0, c as u16, *h)?;
    }
    for (i, a) in job.attempts.iter().enumerate() {
        let row = (i + 1) as u32;
        sheet.write_number(row, 0, a.index as f64)?;
        sheet.write_string(row, 1, a.started.as_str())?;
        sheet.write_string(row, 2, a.ended.as_str())?;
        sheet.write_boolean(row, 3, a.ok)?;
        if let Some(code) = a.exit_code {
            sheet.write_number(row, 4, code as f64)?;
        }
        sheet.write_number(row, 5, a.frames_done as f64)?;
        sheet.write_number(row, 6, a.frames_failed as f64)?;
        sheet.write_string(row, 7, a.log.as_str())?;
    }

    let path = job.data_dir().join("report.xlsx");
    workbook.save(&path)?;
    Ok(path)
}

/// Build every report file for a job and return the xlsx path.
pub fn build_all(job: &Job) -> Result<std::path::PathBuf> {
    let _ = fs::create_dir_all(job.data_dir());
    let _ = frames_csv(job)?;
    let _ = attempts_csv(job)?;
    xlsx(job)
}

pub fn exists(path: &Path) -> bool {
    Path::new(path).exists()
}