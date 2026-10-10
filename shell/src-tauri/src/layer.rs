//! The overlay pill as a Wayland layer-shell surface.
//!
//! A normal window is the wrong tool for the recording pill on Wayland. There
//! is no "always on top", an application cannot place its own window, and the
//! compositor hands keyboard focus to a window as it appears - which would pull
//! focus from the window the words are about to be typed into. A layer-shell
//! surface is what panels, launchers and on-screen displays use: it sits on the
//! overlay layer above every window, is positioned by anchor and margin, and
//! `keyboard-interactivity: none` means it can never take focus.
//!
//! GTK has no layer-shell support of its own; `libgtk-layer-shell` adds it. It
//! is loaded at run time rather than linked, so the app builds and runs without
//! it - the overlay is then simply left out, because a plain window would steal
//! focus. For the library to work it must be loaded before libwayland-client,
//! which is why `preload()` re-executes the program once with LD_PRELOAD set.

use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::os::unix::process::CommandExt;

use gtk::glib::object::ObjectType;
use gtk::prelude::*;

const LIB: &str = "libgtk-layer-shell.so.0";
const MARKER: &str = "PROMPT_MAXXER_LAYER_PRELOADED";

// GtkLayerShellLayer / GtkLayerShellEdge / GtkLayerShellKeyboardMode.
const LAYER_OVERLAY: c_int = 3;
const EDGE_BOTTOM: c_int = 3;
const KEYBOARD_NONE: c_int = 0;

/// Directories searched for the library, for the re-exec. dlopen cannot be
/// used for this: it has to be loaded first, and the point is to know whether
/// it exists before starting.
const SEARCH: &[&str] = &[
    "/usr/lib",
    "/usr/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/local/lib",
];

/// Re-execute with libgtk-layer-shell preloaded. Call first thing in `main`.
///
/// Does nothing when it is already preloaded, on X11, or when the library is
/// not installed. Returns only when the program carries on as it is.
pub fn preload() {
    if std::env::var_os(MARKER).is_some() || std::env::var_os("WAYLAND_DISPLAY").is_none() {
        return;
    }
    let Some(path) = SEARCH
        .iter()
        .map(|dir| std::path::Path::new(dir).join(LIB))
        .find(|path| path.exists())
    else {
        return;
    };
    let Ok(exe) = std::env::current_exe() else { return };

    let mut preload = std::ffi::OsString::from(&path);
    if let Some(existing) = std::env::var_os("LD_PRELOAD").filter(|value| !value.is_empty()) {
        preload.push(" ");
        preload.push(existing);
    }
    // exec only returns on failure, in which case carrying on unpreloaded is
    // the right fallback.
    let _ = std::process::Command::new(exe)
        .args(std::env::args_os().skip(1))
        .env("LD_PRELOAD", preload)
        .env(MARKER, "1")
        .exec();
}

/// Called once the process is running preloaded. The library is loaded into
/// this process now, so keep it out of everything started from here on: the
/// WebKit helper processes and the engine have no use for it.
pub fn clean_environment() {
    if std::env::var_os(MARKER).is_some() {
        std::env::remove_var("LD_PRELOAD");
    }
}

struct Api {
    is_supported: unsafe extern "C" fn() -> c_int,
    init_for_window: unsafe extern "C" fn(*mut c_void),
    set_layer: unsafe extern "C" fn(*mut c_void, c_int),
    set_anchor: unsafe extern "C" fn(*mut c_void, c_int, c_int),
    set_margin: unsafe extern "C" fn(*mut c_void, c_int, c_int),
    set_keyboard_mode: unsafe extern "C" fn(*mut c_void, c_int),
    set_namespace: unsafe extern "C" fn(*mut c_void, *const c_char),
    set_monitor: unsafe extern "C" fn(*mut c_void, *mut c_void),
}

fn symbol(name: &CStr) -> Option<*mut c_void> {
    let found = unsafe { libc::dlsym(libc::RTLD_DEFAULT, name.as_ptr()) };
    (!found.is_null()).then_some(found)
}

fn api() -> Option<Api> {
    macro_rules! load {
        ($name:literal) => {
            // SAFETY: the symbol is a C function with the signature declared in `Api`.
            unsafe { std::mem::transmute(symbol(CStr::from_bytes_with_nul(concat!($name, "\0").as_bytes()).ok()?)?) }
        };
    }
    Some(Api {
        is_supported: load!("gtk_layer_is_supported"),
        init_for_window: load!("gtk_layer_init_for_window"),
        set_layer: load!("gtk_layer_set_layer"),
        set_anchor: load!("gtk_layer_set_anchor"),
        set_margin: load!("gtk_layer_set_margin"),
        set_keyboard_mode: load!("gtk_layer_set_keyboard_mode"),
        set_namespace: load!("gtk_layer_set_namespace"),
        set_monitor: load!("gtk_layer_set_monitor"),
    })
}

/// Make `window` a bottom-centre overlay, `margin_bottom` pixels above the
/// edge of the screen. Returns whether it worked; if not, the window is left
/// untouched and should not be shown.
pub fn pin_overlay(window: &gtk::ApplicationWindow, margin_bottom: i32) -> bool {
    let Some(api) = api() else {
        return false;
    };
    unsafe {
        if (api.is_supported)() == 0 {
            return false;
        }
    }

    // gtk-layer-shell must be set up before the window exists on screen.
    if window.is_realized() {
        window.hide();
        window.unrealize();
    }

    let raw = window.as_ptr() as *mut c_void;
    let namespace = CString::new("prompt-maxxer-overlay").expect("no NUL in a literal");
    unsafe {
        (api.init_for_window)(raw);
        (api.set_layer)(raw, LAYER_OVERLAY);
        // Anchored to the bottom edge only, which centres it horizontally.
        (api.set_anchor)(raw, EDGE_BOTTOM, 1);
        (api.set_margin)(raw, EDGE_BOTTOM, margin_bottom);
        (api.set_keyboard_mode)(raw, KEYBOARD_NONE);
        (api.set_namespace)(raw, namespace.as_ptr());
    }
    true
}

/// Let every pointer event on the overlay fall through to the window behind it.
///
/// A layer-shell surface covers its whole rectangle, transparent or not, and
/// takes the clicks in it unless its input region is empty. The shape tao
/// applies for `set_ignore_cursor_events` is not enough here: on Wayland the
/// surface kept taking clicks, leaving a dead zone the size of the overlay.
/// `set_pass_through` gives the surface a truly empty input region, and it is
/// applied again on every map and resize because GDK rebuilds the region then.
pub fn pass_input_through(window: &gtk::ApplicationWindow) {
    fn apply(window: &gtk::ApplicationWindow) {
        if let Some(surface) = window.window() {
            surface.set_pass_through(true);
        }
    }
    apply(window);
    window.connect_map(apply);
    window.connect_size_allocate(|window, _| apply(window));
}

/// Put the overlay on the monitor whose top-left corner is at (`x`, `y`) in the
/// compositor's coordinates, which is how Hyprland reports each output. Without
/// this the compositor picks the output, and it was not the one with focus.
/// Returns whether a matching monitor was found.
pub fn place_on_monitor(window: &gtk::ApplicationWindow, x: i32, y: i32) -> bool {
    let Some(api) = api() else {
        return false;
    };
    let Some(display) = gtk::gdk::Display::default() else {
        return false;
    };
    for index in 0..display.n_monitors() {
        let Some(monitor) = display.monitor(index) else {
            continue;
        };
        let area = monitor.geometry();
        if area.x() == x && area.y() == y {
            // SAFETY: both pointers are live GObjects for the duration of the call.
            unsafe { (api.set_monitor)(window.as_ptr() as *mut c_void, monitor.as_ptr() as *mut c_void) };
            return true;
        }
    }
    false
}
