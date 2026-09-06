//! Deadline-bounded native recovery helpers. Pipe capture never grows with output.
use std::io::{self, Read};
use std::process::{Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

pub(crate) struct Output {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

const CAP: usize = 8192;

#[cfg(unix)]
fn nonblocking<T: std::os::fd::AsRawFd>(pipe: &T) -> io::Result<()> {
    let fd = pipe.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags == -1 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn drain(reader: &mut impl Read, captured: &mut Vec<u8>) -> io::Result<()> {
    let mut chunk = [0_u8; 4096];
    // A continuously noisy helper must still yield to the deadline check.
    for _ in 0..16 {
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                let count = count.min(CAP.saturating_sub(captured.len()));
                captured.extend_from_slice(&chunk[..count]);
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

pub(crate) fn run(command: &mut Command, timeout: Duration) -> io::Result<Output> {
    let deadline = Instant::now() + timeout;
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let result = (|| {
        let mut stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::other("missing stdout"))?;
        let mut stderr = child
            .stderr
            .take()
            .ok_or_else(|| io::Error::other("missing stderr"))?;
        nonblocking(&stdout)?;
        nonblocking(&stderr)?;
        let mut out = Vec::new();
        let mut err = Vec::new();
        loop {
            drain(&mut stdout, &mut out)?;
            drain(&mut stderr, &mut err)?;
            if let Some(status) = child.try_wait()? {
                drain(&mut stdout, &mut out)?;
                drain(&mut stderr, &mut err)?;
                return Ok(Output {
                    status,
                    stdout: out,
                    stderr: err,
                });
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "recovery helper timed out",
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    })();
    // Kill the owned group as well as the leader: a helper may fork children which
    // retain pipe handles. No reader threads can remain blocked on those handles.
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    if result.is_err() {
        let _ = child.kill();
    }
    let _ = child.wait();
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noisy_and_hung_helpers_are_bounded_and_reaped() {
        let start = Instant::now();
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "while :; do printf 'noise'; printf 'error' >&2; done"]);
        let error = run(&mut command, Duration::from_millis(80)).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(start.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn captures_bounded_success_and_failure_output() {
        for exit in ["0", "7"] {
            let mut command = Command::new("/bin/sh");
            command.args(["-c", &format!("i=0; while [ $i -lt 2000 ]; do printf '0123456789'; i=$((i+1)); done; printf 'detail' >&2; exit {exit}")]);
            let output = run(&mut command, Duration::from_secs(2)).unwrap();
            assert_eq!(output.status.success(), exit == "0");
            assert_eq!(output.stdout.len(), CAP);
            assert_eq!(output.stderr, b"detail");
        }
    }

    #[test]
    fn descendants_holding_pipes_do_not_strand_completion() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "sleep 60 & printf done"]);
        let start = Instant::now();
        let output = run(&mut command, Duration::from_millis(200)).unwrap();
        assert!(output.status.success());
        assert!(start.elapsed() < Duration::from_secs(1));
    }
}
