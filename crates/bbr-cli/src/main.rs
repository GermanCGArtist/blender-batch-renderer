use bbr_core::models::Job;
use std::path::Path;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("show") if args.len() == 2 => match Job::load(Path::new(&args[1])) {
            Some(j) => {
                let done = j.count("done");
                let total = j.frame_list().map(|f| f.len().to_string()).unwrap_or("?".into());
                println!("{}  [{}]  {}/{} frames", j.name, j.status, done, total);
            }
            None => { eprintln!("no readable job.json in {}", args[1]); std::process::exit(1); }
        },
        _ => eprintln!("bbr (Rust V2, alpha)\nusage: bbr show <job folder>"),
    }
}
