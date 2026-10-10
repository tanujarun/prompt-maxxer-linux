//! Start at login: an XDG autostart entry, `~/.config/autostart/prompt-maxxer.desktop`.
//!
//! Written as a plain file rather than through a plugin, because the switch in
//! settings has to tell the truth about two things the file alone does not
//! show. The entry can be switched off without being removed (`Hidden=true`, or
//! `X-GNOME-Autostart-enabled=false`, which is what desktop settings tools
//! write), and it may launch a different copy of the app than the one running.

use serde::Serialize;
use std::{fs, io, path::{Path, PathBuf}};

const FILE_NAME: &str = "prompt-maxxer.desktop";

/// Added to the command run at login, so that launch can be told apart from
/// one the user started.
pub const LAUNCH_ARG: &str = "--autostart";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Status {
    /// The desktop will launch this copy of the app at login.
    pub enabled: bool,
    /// An entry exists, but it was switched off outside this app.
    pub disabled_by_system: bool,
    /// An entry exists, but it launches a different copy of the app.
    pub other_copy: bool,
}

const OFF: Status = Status { enabled: false, disabled_by_system: false, other_copy: false };

fn entry_path() -> PathBuf {
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
        .unwrap_or_else(|| PathBuf::from(".config"));
    config.join("autostart").join(FILE_NAME)
}

/// The `Exec` value for this copy of the app. Quoted, because the path may
/// contain spaces, and `%` doubled, because it starts a field code.
fn this_command() -> String {
    let exe = std::env::current_exe().unwrap_or_default();
    command_for(&exe)
}

fn command_for(exe: &Path) -> String {
    let mut quoted = String::from("\"");
    for ch in exe.display().to_string().chars() {
        match ch {
            '"' | '`' | '$' | '\\' => {
                quoted.push('\\');
                quoted.push(ch);
            }
            '%' => quoted.push_str("%%"),
            _ => quoted.push(ch),
        }
    }
    quoted.push('"');
    format!("{quoted} {LAUNCH_ARG}")
}

pub fn status() -> Status {
    status_of(&entry_path(), &this_command())
}

pub fn set(enabled: bool) -> Result<Status, String> {
    let path = entry_path();
    let command = this_command();
    set_for(&path, &command, enabled)
        .map_err(|err| format!("Could not update the autostart entry: {err}"))?;
    Ok(status_of(&path, &command))
}

fn key<'a>(contents: &'a str, name: &str) -> Option<&'a str> {
    contents
        .lines()
        .filter_map(|line| line.split_once('='))
        .find(|(k, _)| k.trim() == name)
        .map(|(_, v)| v.trim())
}

fn status_of(path: &Path, command: &str) -> Status {
    let Ok(contents) = fs::read_to_string(path) else {
        return OFF;
    };
    let ours = key(&contents, "Exec") == Some(command);
    let disabled_by_system = key(&contents, "Hidden") == Some("true")
        || key(&contents, "X-GNOME-Autostart-enabled") == Some("false");
    Status { enabled: ours && !disabled_by_system, disabled_by_system, other_copy: !ours }
}

fn set_for(path: &Path, command: &str, enabled: bool) -> io::Result<()> {
    if !enabled {
        return match fs::remove_file(path) {
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
            other => other,
        };
    }
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    // Written fresh, which also clears any switch-off recorded in the old file.
    fs::write(
        path,
        format!(
            "[Desktop Entry]\n\
             Type=Application\n\
             Name=Prompt Maxxer\n\
             Comment=Push-to-talk dictation\n\
             Exec={command}\n\
             Icon=prompt-maxxer\n\
             Terminal=false\n\
             Categories=Utility;\n\
             X-GNOME-Autostart-enabled=true\n"
        ),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn switches_on_and_off_and_reports_what_the_desktop_will_do() {
        let dir = std::env::temp_dir().join(format!("pm-autostart-test-{}", std::process::id()));
        let path = dir.join("autostart").join(FILE_NAME);
        let command = command_for(Path::new("/opt/Prompt Maxxer/prompt-maxxer"));
        assert_eq!(command, r#""/opt/Prompt Maxxer/prompt-maxxer" --autostart"#);

        assert_eq!(status_of(&path, &command), OFF);

        set_for(&path, &command, true).unwrap();
        assert!(status_of(&path, &command).enabled, "enabled after turning on");

        let elsewhere = status_of(&path, r#""/home/me/other/prompt-maxxer" --autostart"#);
        assert!(!elsewhere.enabled && elsewhere.other_copy, "detects another copy");

        let written = fs::read_to_string(&path).unwrap();
        fs::write(&path, written.replace("Autostart-enabled=true", "Autostart-enabled=false")).unwrap();
        let blocked = status_of(&path, &command);
        assert!(!blocked.enabled && blocked.disabled_by_system, "detects it being switched off");

        set_for(&path, &command, true).unwrap();
        assert!(status_of(&path, &command).enabled, "turning it on clears the switch");

        set_for(&path, &command, false).unwrap();
        assert_eq!(status_of(&path, &command), OFF, "off again, with nothing left behind");
        set_for(&path, &command, false).unwrap(); // turning off twice is fine

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn escapes_what_the_exec_key_treats_specially() {
        assert_eq!(
            command_for(Path::new("/a/100%/b$c")),
            r#""/a/100%%/b\$c" --autostart"#
        );
    }
}
