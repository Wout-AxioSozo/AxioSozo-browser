use browser_core::{Coordinator, Envelope, MAX_FRAME};
use std::io::{self, BufRead, Read, Write};
use std::os::fd::FromRawFd;
use std::time::Instant;

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Bootstrap {
    version: u32,
    token: String,
    session_id: String,
}

fn read_frame(input: &mut impl BufRead, limit: usize) -> io::Result<Option<Vec<u8>>> {
    let mut frame = Vec::new();
    let count = input
        .take((limit + 1) as u64)
        .read_until(b'\n', &mut frame)?;
    if count == 0 {
        return Ok(None);
    }
    if count > limit || frame.last() != Some(&b'\n') {
        return Err(io::Error::other("invalid or oversized IPC frame"));
    }
    Ok(Some(frame))
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut input = stdin.lock();
    if let Some(mode) = std::env::var_os("AXIOSOZO_BOOTSTRAP_MODE") {
        if mode != "stdin-v1" {
            return Err("unknown bootstrap mode".into());
        }
    }
    let (token, session) = if std::env::var("AXIOSOZO_BOOTSTRAP_MODE").as_deref() == Ok("stdin-v1")
    {
        if std::env::var_os("AXIOSOZO_BOOTSTRAP_FD").is_some() {
            return Err("ambiguous bootstrap mode".into());
        }
        // Gecko Subprocess exposes owned standard pipes, not arbitrary inherited
        // descriptors. Its parent sends this bounded first frame over that pipe.
        let frame = read_frame(&mut input, 1024)?.ok_or("missing bootstrap")?;
        let bootstrap: Bootstrap = serde_json::from_slice(&frame)?;
        if bootstrap.version != 1 {
            return Err("invalid bootstrap version".into());
        }
        (bootstrap.token, bootstrap.session_id)
    } else {
        // The owning parent passes a dedicated anonymous pipe. Never take a token on argv,
        // an environment variable, TCP, or a discoverable file in the profile.
        let raw_fd: i32 = std::env::var("AXIOSOZO_BOOTSTRAP_FD")?.parse()?;
        if raw_fd < 3 {
            return Err("bootstrap must be a separate inherited pipe".into());
        }
        // SAFETY: this child takes exclusive ownership of the explicitly inherited fd.
        let mut bootstrap = unsafe { std::fs::File::from_raw_fd(raw_fd) };
        let mut secret = [0; 64];
        bootstrap.read_exact(&mut secret)?;
        drop(bootstrap);
        (
            String::from_utf8(secret.to_vec())?,
            std::env::var("AXIOSOZO_SESSION_ID")?,
        )
    };
    let mut coordinator = Coordinator::new(token, session).map_err(io::Error::other)?;
    let epoch = Instant::now();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    while let Some(frame) = read_frame(&mut input, MAX_FRAME)? {
        // Decode errors terminate the untrusted stream; never echo malformed input/secrets.
        let envelope: Envelope =
            serde_json::from_slice(&frame).map_err(|_| "invalid IPC schema")?;
        let response = coordinator.handle(envelope, epoch.elapsed().as_millis().try_into()?);
        serde_json::to_writer(&mut output, &response)?;
        writeln!(&mut output)?;
        output.flush()?;
        if response.status == "shutdown" {
            break;
        }
    }
    Ok(())
}

fn main() {
    if run().is_err() {
        eprintln!("browser-core: rejected invalid bootstrap or IPC; session closed");
        std::process::exit(2);
    }
}
