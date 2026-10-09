"""Repair build scripts that test the build host instead of Cargo's target."""
import hashlib
import json
from pathlib import Path
import sys

vendor = Path(sys.argv[1])

# Configure the updater only in the OBS build tree; leave repository source
# unchanged for native builds. build-windows.sh provides this public setting.
updater = Path('apps/desktop/src-tauri/src/app_updates.rs')
old_endpoint = '''const UPDATE_ENDPOINT: &str =
    "https://github.com/Dreaminko/VRCS/releases/latest/download/latest.json";'''
updater_source = updater.read_text()
if updater_source.count(old_endpoint) != 1:
    raise SystemExit('Updater endpoint patch did not apply: ' + str(updater))
updater.write_text(updater_source.replace(old_endpoint, 'const UPDATE_ENDPOINT: &str = env!("TAURI_UPDATER_ENDPOINT");'))

def edit(crate, relative, transform):
    base = vendor / crate
    path = base / relative
    old = path.read_text()
    new = transform(old)
    if old == new:
        raise SystemExit('Cross-build patch did not apply: ' + str(path))
    path.write_text(new)
    checksum = base / '.cargo-checksum.json'
    doc = json.loads(checksum.read_text())
    doc['files'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    checksum.write_text(json.dumps(doc))

def whisper(text):
    start = text.index('        cfg_if::cfg_if! {', text.index('#[cfg(feature = "cuda")]'))
    end = text.index('\n    #[cfg(feature = "hipblas")]', start)
    text = text[:start] + '''        let cuda_path = PathBuf::from(env::var("CUDA_PATH").unwrap()).join("lib/x64");
        println!("cargo:rustc-link-search={}", cuda_path.display());
    }
''' + text[end:]
    text = text.replace('if cfg!(target_os = "windows") {\n        config.cxxflag("/utf-8");',
                        'if target.contains("windows") {\n        config.cxxflag("-finput-charset=UTF-8");')
    text = text.replace('if cfg!(windows) {\n            println!("cargo:rerun-if-env-changed=VULKAN_SDK");',
                        'if target.contains("windows") {\n            println!("cargo:rerun-if-env-changed=VULKAN_SDK");')
    text = text.replace('            let vulkan_lib_path = vulkan_path.join("Lib");',
                        '            let vulkan_lib_path = vulkan_path.join("lib");')
    text = text.replace('.define("CMAKE_CUDA_FLAGS", "-Xcompiler=-fPIC");',
                        '.define("GGML_NATIVE", "OFF");')
    return text

edit('whisper-rs-sys','build.rs',whisper)
edit('whisper-rs-sys','whisper.cpp/ggml/CMakeLists.txt',
     lambda s:s.replace('# remove the lib prefix on win32 mingw\nif (WIN32)',
                        '# Keep the GNU lib prefix so Rust finds MinGW static archives.\nif (WIN32 AND NOT MINGW)'))
edit('openvr_sys','build.rs',lambda s:s.replace('config.cxxflag("/DWIN32");','config.cxxflag("-DWIN32");')
     .replace('.header("wrapper.hpp")', '.header("wrapper.hpp")\n        .allowlist_file(".*openvr_capi.h")\n        .allowlist_file(".*wrapper.hpp")'))
edit('openvr_sys','openvr/headers/openvr_capi.h',
     lambda s:s.replace('#if defined( __WIN32 )\ntypedef char bool;',
                        '#if defined( __WIN32 ) && !defined(__cplusplus)\ntypedef char bool;'))
edit('whisper-rs-sys','whisper.cpp/ggml/src/ggml-cuda/CMakeLists.txt',
     lambda s:s.replace('    target_compile_options(ggml-cuda PRIVATE',
                        '''    # nvcc driver flags cannot be passed to the LLVM CUDA compiler.
    if (CMAKE_CUDA_COMPILER_ID STREQUAL "Clang")
        # Keep NaN/Infinity semantics used by softmax and attention masks.
        set(CUDA_FLAGS -ffp-contract=fast -fgpu-approx-transcendentals -fcuda-flush-denormals-to-zero)
    endif()
    target_compile_options(ggml-cuda PRIVATE'''))
edit('whisper-rs-sys','whisper.cpp/ggml/src/ggml-cuda/vecdotq.cuh',
     lambda s:s.replace('iq1m_scale_t scale;', 'iq1m_scale_t scale{};'))
edit('whisper-rs-sys','whisper.cpp/ggml/src/ggml-cuda/convert.cu',
     lambda s:s.replace('iq1m_scale_t scale;', 'iq1m_scale_t scale{};'))
edit('whisper-rs-sys','whisper.cpp/ggml/src/ggml-cuda/unary.cu',
     # Explicit float wrappers avoid GCC cmath's builtin overloads becoming
     # unresolved device functions with the Windows GNU CUDA ABI.
     lambda s:s.replace('return round(x);', 'return roundf(x);')
               .replace('return trunc(x);', 'return truncf(x);'))

# MSVC binds positive OpenVR enums as i32; MinGW binds them as u32.
# Carry the generated ABI type through the application's tracking interfaces.
app = Path.cwd() / 'apps/desktop/src-tauri/src/vr_overlay'
for name in ['backend.rs', 'ocr_input.rs', 'ocr_capture.rs', 'ocr_wrist.rs',
             'ocr_selection.rs', 'ocr_runtime.rs']:
    path = app / name
    old = path.read_text()
    new = old.replace('origin: i32', 'origin: openvr_sys::ETrackingUniverseOrigin')
    new = new.replace('([[f32; 4]; 3], u32, i32)',
                      '([[f32; 4]; 3], u32, openvr_sys::ETrackingUniverseOrigin)')
    new = new.replace('([EyeCapture; 2], u32, i32)',
                      '([EyeCapture; 2], u32, openvr_sys::ETrackingUniverseOrigin)')
    new = new.replace('([[f32; 4]; 3], i32)',
                      '([[f32; 4]; 3], openvr_sys::ETrackingUniverseOrigin)')
    new = new.replace('event.eventType as i32', 'event.eventType as openvr_sys::EVREventType')
    new = new.replace('Option<Result<u32, i32>>',
                      'Option<Result<u32, openvr_sys::EVRInputError>>')
    if old == new:
        raise SystemExit('Application ABI patch did not apply: ' + str(path))
    path.write_text(new)
