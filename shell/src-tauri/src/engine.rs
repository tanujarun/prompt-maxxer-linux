//! Starting and supervising the Python engine.
//!
//! The shell is the thing a user launches, so it is the shell's job to make
//! sure the engine is running. Telling someone to open a terminal and run a
//! package-manager script is a developer instruction, not a product.
//!
//! The engine is still a separate process - dictation must survive the UI
//! crashing - but it is a child of the shell, started on launch and stopped
//! when the user quits from the tray.

use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const ENGINE_ADDR: &str = "127.0.0.1:8765";

/// Handle to an engine we started ourselves. An engine that was already
/// running when we launched is left alone, and stays running when we quit.
#[derive(Default)]
pub struct EngineProcess {
    child: Mutex<Option<Child>>,
}

/// A reachable port is the only honest test that the engine is up: it may have
/// been started by hand, by a previous shell, or by the launcher script.
pub fn is_running() -> bool {
    let Ok(addr) = ENGINE_ADDR.parse::<SocketAddr>() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

/// The Python that has the engine installed: `PROMPT_MAXXER_PYTHON`, else the
/// virtualenv `install.sh` creates under the XDG data directory.
fn locate() -> Option<PathBuf> {
    if let Some(custom) = std::env::var_os("PROMPT_MAXXER_PYTHON") {
        return Some(PathBuf::from(custom));
    }
    let data = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share")))?;
    let python = data.join("prompt-maxxer/venv/bin/python");
    python.is_file().then_some(python)
}

/// The folder the engine's code is in, if that folder is not there.
///
/// `install.sh` installs the engine as a link to the checkout (`pip install -e`),
/// not as a copy, so the engine can only start while the checkout is reachable.
/// When it sits on another drive, that drive may not be mounted yet at login.
/// An engine installed as a copy has no such folder to lose: `None`.
fn missing_source(python: &Path) -> Option<PathBuf> {
    let lib = python.parent()?.parent()?.join("lib");
    for version in std::fs::read_dir(lib).ok()?.flatten() {
        let Ok(packages) = std::fs::read_dir(version.path().join("site-packages")) else {
            continue;
        };
        for entry in packages.flatten() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with("__editable___prompt_maxxer_engine") && name.ends_with("_finder.py") {
                let finder = std::fs::read_to_string(entry.path()).ok()?;
                let source = PathBuf::from(mapped_folder(&finder)?);
                return (!source.is_dir()).then_some(source);
            }
        }
    }
    None
}

/// The folder setuptools recorded for the `prompt_maxxer` package in the finder
/// module of a linked install: `MAPPING: dict[str, str] = {'prompt_maxxer': '/path'}`.
fn mapped_folder(finder: &str) -> Option<&str> {
    let line = finder.lines().find(|line| line.starts_with("MAPPING"))?;
    let (_, value) = line.split_once("'prompt_maxxer': ")?;
    let quote = value.chars().next().filter(|c| matches!(c, '\'' | '"'))?;
    value[1..].split(quote).next()
}

fn describe_missing() -> String {
    concat!(
        "Could not find the Prompt Maxxer engine. Run ./install.sh from the project folder, ",
        "or set PROMPT_MAXXER_PYTHON to a Python that has the engine installed."
    )
    .to_string()
}

/// Start the engine unless something is already listening.
pub fn ensure_running(state: &EngineProcess) -> Result<bool, String> {
    if is_running() {
        return Ok(false);
    }

    // Do not stack children if a previous spawn is still warming up: loading
    // the model takes several seconds, during which the port is not yet open.
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.as_mut() {
            match child.try_wait() {
                Ok(None) => return Ok(false), // still starting
                _ => *guard = None,           // exited; fall through and retry
            }
        }
    }

    let python = locate().ok_or_else(describe_missing)?;
    if let Some(folder) = missing_source(&python) {
        return Err(format!(
            "The engine's code is in {}, which is not there. Is its drive mounted? \
             Dictation starts by itself once it is.",
            folder.display()
        ));
    }
    let child = spawn(&python)?;
    if let Ok(mut guard) = state.child.lock() {
        *guard = Some(child);
    }
    Ok(true)
}

fn spawn(python: &PathBuf) -> Result<Child, String> {
    let mut cmd = Command::new(python);
    // `--die-with-parent` makes the engine exit when this pipe closes, which the
    // kernel does when the shell dies however it dies, even if it is killed.
    // That matters: an engine that outlived the shell would keep reading the
    // keyboard, and with `hotkey.suppress` on, keep one grabbed, with no
    // interface left to stop it. The pipe is held by the `Child` handle.
    cmd.args(["-m", "prompt_maxxer", "--die-with-parent"])
        .stdin(Stdio::piped())
        // The warning is noise the user cannot act on.
        .env("HF_HUB_DISABLE_SYMLINKS_WARNING", "1");

    cmd.spawn()
        .map_err(|err| format!("Could not start the engine: {err}"))
}

/// Stop an engine we started. One we merely found is left running.
pub fn shutdown(state: &EngineProcess) {
    let Ok(mut guard) = state.child.lock() else { return };
    let Some(mut child) = guard.take() else { return };

    // Ask politely first: SIGTERM lets the engine hand its keyboards back
    // cleanly. Closing its stdin has the same effect; do both, then insist.
    drop(child.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_code_folder_of_a_linked_install() {
        let finder = "from __future__ import annotations\n\
                      MAPPING: dict[str, str] = {'prompt_maxxer': '/run/media/me/Disk/AI Projects/engine/prompt_maxxer'}\n\
                      NAMESPACES: dict[str, list[str]] = {}\n";
        assert_eq!(mapped_folder(finder), Some("/run/media/me/Disk/AI Projects/engine/prompt_maxxer"));
        // Python writes a path that contains an apostrophe in double quotes.
        let quoted = "MAPPING: dict[str, str] = {'prompt_maxxer': \"/mnt/it's here/prompt_maxxer\"}";
        assert_eq!(mapped_folder(quoted), Some("/mnt/it's here/prompt_maxxer"));
        assert_eq!(mapped_folder("MAPPING: dict[str, str] = {}"), None);
    }
}
