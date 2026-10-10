//! Prompt Maxxer shell.
//!
//! The shell is deliberately thin. The Python engine owns the dictation hot
//! path - hotkey, capture, recognition, injection - so everything here is
//! presentation: a tray icon, a settings window, and a transparent overlay
//! pill that mirrors engine state over a localhost WebSocket.
//!
//! If this process dies, dictation keeps working.

mod autostart;
mod engine;
mod layer;

use engine::EngineProcess;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder,
    WindowEvent, Wry,
};

/// Where the engine publishes its event stream.
const ENGINE_WS: &str = "ws://127.0.0.1:8765";

pub(crate) const TRAY_ID: &str = "prompt-maxxer-tray";

/// Sections of the settings window that can be opened directly.
#[derive(Clone, Copy)]
enum View {
    Session,
    Models,
}

impl View {
    fn as_str(self) -> &'static str {
        match self {
            View::Session => "session",
            View::Models => "models",
        }
    }

    /// `--settings` opens the settings window, `--models` opens it on models.
    fn from_args<I, S>(args: I) -> Option<View>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut view = None;
        for arg in args {
            match arg.as_ref() {
                "--settings" => view = view.or(Some(View::Session)),
                "--models" => view = Some(View::Models),
                _ => {}
            }
        }
        view
    }
}

#[tauri::command]
fn engine_endpoint() -> String {
    ENGINE_WS.to_string()
}

/// Whether anything is listening on the engine port.
#[tauri::command]
fn engine_running() -> bool {
    engine::is_running()
}

/// Start the engine on demand, for the button the settings window shows when
/// it cannot reach one.
#[tauri::command]
fn start_engine(state: State<'_, EngineProcess>) -> Result<bool, String> {
    engine::ensure_running(&state)
}

/// Whether the desktop will launch this copy of the app at login.
#[tauri::command]
fn autostart_status() -> autostart::Status {
    autostart::status()
}

/// Turn launching at login on or off for this copy of the app.
#[tauri::command]
fn set_autostart(enabled: bool) -> Result<autostart::Status, String> {
    autostart::set(enabled)
}

/// Keep the tray tooltip in step with the push-to-talk key. The overlay calls
/// this whenever the engine announces its binding, including after a rebind.
#[tauri::command]
fn set_hotkey_label(app: tauri::AppHandle, label: String) -> Result<(), String> {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_tooltip(Some(format!("Prompt Maxxer — hold {label} to dictate")))
            .map_err(|err| err.to_string())?;
    }
    Ok(())
}

/// Surface size while the pill is showing, and while it idles.
///
/// The overlay stays mapped for the life of the app and changes size instead.
/// Two things rule out the alternatives. A mapped layer-shell surface takes
/// pointer input over its whole rectangle, transparent or not, so a full-size
/// one left an invisible dead zone; a 1 px one is harmless. And mapping or
/// unmapping a layer makes Hyprland re-check which window is under the pointer,
/// which can move keyboard focus to it in the middle of typing, so that must not
/// happen on every dictation.
const OVERLAY_ACTIVE: (i32, i32) = (700, 120);
const OVERLAY_IDLE: (i32, i32) = (1, 1);

/// Top-left corner of the monitor the overlay is currently on.
static OVERLAY_MONITOR: std::sync::Mutex<Option<(i32, i32)>> = std::sync::Mutex::new(None);

/// Top-left corner of the monitor that has focus, from Hyprland.
fn focused_monitor() -> Option<(i32, i32)> {
    let output = std::process::Command::new("hyprctl").args(["-j", "monitors"]).output().ok()?;
    let monitors: serde_json::Value = serde_json::from_slice(&output.stdout).ok()?;
    monitors.as_array()?.iter().find_map(|monitor| {
        if monitor["focused"].as_bool() != Some(true) {
            return None;
        }
        Some((monitor["x"].as_i64()? as i32, monitor["y"].as_i64()? as i32))
    })
}

fn resize_overlay(app: &tauri::AppHandle, active: bool) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        use gtk::prelude::*;
        let Some(overlay) = handle.get_webview_window("overlay") else {
            return;
        };
        let Ok(window) = overlay.gtk_window() else {
            return;
        };
        if active {
            // The pill goes on the monitor that has focus, not wherever the
            // compositor last put it. Moving it remaps the surface, so only do
            // so when that monitor has changed.
            if let Some(origin) = focused_monitor() {
                if let Ok(mut current) = OVERLAY_MONITOR.lock() {
                    if *current != Some(origin) && layer::place_on_monitor(&window, origin.0, origin.1) {
                        *current = Some(origin);
                    }
                }
            }
        }
        let (width, height) = if active { OVERLAY_ACTIVE } else { OVERLAY_IDLE };
        window.set_size_request(width, height);
        window.resize(width, height);
    });
}

/// Grow the overlay to the pill's size. The pill calls this each time it appears.
#[tauri::command]
fn raise_overlay(app: tauri::AppHandle) {
    resize_overlay(&app, true);
}

/// Shrink the overlay to a point once the pill has faded out.
#[tauri::command]
fn lower_overlay(app: tauri::AppHandle) {
    resize_overlay(&app, false);
}

/// Whether the engine has never run for this user: it writes its config file
/// on first start.
fn is_first_run() -> bool {
    let config_home = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .filter(|path| path.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".config")));
    config_home
        .map(|dir| dir.join("prompt-maxxer").join("config.json"))
        .is_some_and(|config| !config.is_file())
}

/// How often to look again while the engine is not up.
const ENGINE_POLL: std::time::Duration = std::time::Duration::from_secs(1);

/// Starts that may come to nothing before the shell stops trying by itself.
const ENGINE_STARTS: u32 = 5;

/// Start the engine, waiting first for whatever it needs to be there.
///
/// One attempt at launch is not enough. At login the shell can start before
/// the drive that holds the engine's code is mounted, and dictation would stay
/// off until someone started the engine by hand, as a process the shell does
/// not own. So this waits for the code and starts the engine when it appears.
fn keep_starting_engine(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut starts = 0;
        let mut reported: Option<String> = None;
        while !engine::is_running() {
            if starts == ENGINE_STARTS {
                eprintln!("prompt-maxxer: the engine did not come up in {ENGINE_STARTS} starts; not trying again");
                return;
            }
            let wait = match engine::ensure_running(&app.state::<EngineProcess>()) {
                // Started. Leave longer each time before starting it again.
                Ok(true) => {
                    starts += 1;
                    ENGINE_POLL * (1 << starts)
                }
                Ok(false) => ENGINE_POLL,
                // Nothing was started, because its code or its Python is not
                // there, so looking again is cheap. Say why once, then wait.
                Err(err) => {
                    if reported.as_deref() != Some(err.as_str()) {
                        eprintln!("prompt-maxxer: {err}");
                        reported = Some(err);
                    }
                    ENGINE_POLL
                }
            };
            std::thread::sleep(wait);
        }
    });
}

/// Distance from the bottom edge of the screen to the pill.
const OVERLAY_MARGIN_BOTTOM: i32 = 110;

/// Make the overlay a bottom-centre layer-shell surface, mapped at its idle
/// size. The pill grows it when it needs it (`raise_overlay`).
///
/// Without layer-shell support the overlay is never shown. As an ordinary
/// window it could not be placed or kept on top, and it would take keyboard
/// focus the moment it appeared, away from the window being dictated into.
fn place_overlay(app: &tauri::AppHandle) -> tauri::Result<()> {
    let Some(overlay) = app.get_webview_window("overlay") else {
        return Ok(());
    };

    let window = overlay.gtk_window()?;
    if !layer::pin_overlay(&window, OVERLAY_MARGIN_BOTTOM) {
        eprintln!(
            "prompt-maxxer: the recording overlay needs libgtk-layer-shell \
             (sudo pacman -S gtk-layer-shell) on a compositor that supports it; running without it"
        );
        overlay.destroy()?;
        return Ok(());
    }

    // The pill must never intercept input; the user is typing into the window
    // behind it.
    layer::pass_input_through(&window);

    // Mapped once, here, at its idle size, and never unmapped (see OVERLAY_IDLE).
    {
        use gtk::prelude::*;
        window.set_size_request(OVERLAY_IDLE.0, OVERLAY_IDLE.1);
        window.resize(OVERLAY_IDLE.0, OVERLAY_IDLE.1);
    }
    overlay.show()?;
    Ok(())
}

/// Show the settings window on a given section, creating it on first use.
///
/// Every window costs its own WebKit renderer. Creating this one eagerly and
/// hiding it measured at roughly 150 MB of resident memory for a window most
/// sessions never open, which is a poor trade for a tray app that runs all day.
fn show_settings(app: &tauri::AppHandle, view: View) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.eval(format!("window.__pmShowView?.({:?})", view.as_str()));
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
        return;
    }

    match WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Prompt Maxxer")
        // The compositor owns window management; the GTK titlebar with its
        // minimize/maximize/close buttons is dead weight on a tiling desktop.
        .decorations(false)
        .inner_size(720.0, 800.0)
        // Small enough to tile freely; the sections scroll when it is short.
        .min_inner_size(420.0, 320.0)
        .center()
        // Runs before the page's own scripts, so the window opens on the
        // requested section instead of flashing the default one first.
        .initialization_script(format!("window.__PM_INITIAL_VIEW = {:?};", view.as_str()))
        .build()
    {
        Ok(win) => {
            let _ = win.set_focus();
        }
        Err(err) => eprintln!("could not open settings window: {err}"),
    }
}

fn tray_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let models = MenuItem::with_id(app, "models", "Models…", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Prompt Maxxer", true, None::<&str>)?;
    Menu::with_items(app, &[&settings, &models, &separator, &quit])
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let menu = tray_menu(app)?;

    let icon = app
        .default_window_icon()
        .cloned()
        .expect("bundle always provides a default window icon");

    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon)
        .tooltip("Prompt Maxxer — hold Right Ctrl to dictate")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            // Left click opens settings; the menu stays on right click. A tray
            // icon that only responds to right click is easy to miss entirely.
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_settings(tray.app_handle(), View::Session);
            }
        })
        .on_menu_event(|app, event| match event.id.as_ref() {
            "settings" => show_settings(app, View::Session),
            "models" => show_settings(app, View::Models),
            "quit" => {
                engine::shutdown(&app.state::<EngineProcess>());
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;

    Ok(())
}

/// Entry point: sort out the environment, then run the app.
pub fn start() {
    layer::preload();
    layer::clean_environment();

    // WebKitGTK's DMA-BUF renderer shows a blank window on some NVIDIA
    // drivers under Wayland. Left to the user if they set it themselves.
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }

    run()
}

pub fn run() {
    tauri::Builder::default()
        // Registered first on purpose: a second launch is intercepted here and
        // exits before it can create a tray icon, start another engine or open
        // windows of its own. The copy already running shows its settings.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            show_settings(app, View::from_args(&args).unwrap_or(View::Session));
        }))
        .plugin(tauri_plugin_opener::init())
        .manage(EngineProcess::default())
        .invoke_handler(tauri::generate_handler![
            engine_endpoint,
            engine_running,
            start_engine,
            set_hotkey_label,
            raise_overlay,
            lower_overlay,
            autostart_status,
            set_autostart
        ])
        .setup(|app| {
            // On this desktop the tanuj.maxxer bar widget is the app's icon and
            // its card holds the tray's actions (settings, models, quit), so the
            // tray icon would be a second one. It stays available with `--tray`.
            if std::env::args().any(|arg| arg == "--tray") {
                build_tray(app.handle())?;
            }
            place_overlay(app.handle())?;

            // Checked before the engine starts, because the engine creates the
            // config file as soon as it runs.
            let first_run = is_first_run();

            // Launching the shell should be enough to make dictation work.
            keep_starting_engine(app.handle().clone());

            if let Some(view) = View::from_args(std::env::args()) {
                show_settings(app.handle(), view);
            } else if first_run {
                // First launch on this machine: put the model download and the
                // push-to-talk key in front of the person, rather than having
                // it all happen silently in the tray.
                show_settings(app.handle(), View::Session);
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the settings window destroys it, freeing its renderer;
            // the tray rebuilds it on demand. Either way the close button must
            // not end the session - Prompt Maxxer is a tray app.
            if let WindowEvent::CloseRequested { .. } = event {
                if window.label() == "overlay" {
                    // The overlay is click-through and has no close affordance,
                    // but guard against anything else closing it.
                    return;
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Prompt Maxxer")
        .run(|app, event| {
            // Covers the paths that do not go through the tray Quit item.
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                engine::shutdown(&app.state::<EngineProcess>());
            }
        });
}
