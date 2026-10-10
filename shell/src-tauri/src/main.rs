// The recording overlay needs libgtk-layer-shell loaded ahead of libwayland, so
// this may re-execute the program once, before anything else runs.
fn main() {
    prompt_maxxer_lib::start()
}
