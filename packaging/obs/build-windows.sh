#!/bin/bash
set -euo pipefail
variant=$1
version=$2
sources=$3
root=$(pwd)
obs="$root/.obs"
trap 'status=$?; printf "Build failed at line %s: %s (exit %s)\n" "$LINENO" "$BASH_COMMAND" "$status" >&2; find "$obs" -name CMakeConfigureLog.yaml -exec tail -n 160 {} \; >&2; exit "$status"' ERR
target=x86_64-pc-windows-gnu
cross=x86_64-w64-mingw32
sysroot=/usr/x86_64-w64-mingw32/sys-root/mingw
build_jobs=$(nproc)

for component in rustc-1.99.0-x86_64-unknown-linux-gnu cargo-1.99.0-x86_64-unknown-linux-gnu \
                 rust-std-1.99.0-x86_64-unknown-linux-gnu rust-std-1.99.0-x86_64-pc-windows-gnu; do
    bash "$obs/$component/install.sh" --prefix="$obs/rust" --disable-ldconfig >/dev/null
done
export PATH="$obs/rust/bin:$PATH"
scons -C "$obs/nsis-src" -j "$build_jobs" SKIPSTUBS=all SKIPPLUGINS=all \
    SKIPUTILS=all SKIPMISC=all VERSION=3.11 PREFIX="$obs/nsis-compiler" install-compiler
export PATH="$obs/nsis-compiler/bin:$PATH"
export NSISDIR="$obs/tauri-tools/NSIS"
# POSIX makensis uses its compiled data path; point it at the CI stubs/includes.
mkdir -p "$obs/nsis-compiler/share"
ln -s "$NSISDIR" "$obs/nsis-compiler/share/nsis"
export NSIS_PATH="$NSISDIR"
makensis -VERSION
test "$(makensis -VERSION)" = v3.11
export CARGO_HOME="$obs/cargo-home"
export CARGO_NET_OFFLINE=true
export CARGO_TARGET_DIR="$obs/target-$variant"
export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER="$cross-gcc"
export CC_x86_64_pc_windows_gnu="$cross-gcc"
export CXX_x86_64_pc_windows_gnu="$cross-g++"
export AR_x86_64_pc_windows_gnu="$cross-ar"
export CMAKE_GENERATOR=Ninja
export GGML_NATIVE=OFF
export GGML_ALL_WARNINGS=OFF
export CMAKE_BUILD_PARALLEL_LEVEL="$build_jobs"
export BINDGEN_EXTRA_CLANG_ARGS_x86_64_pc_windows_gnu="--target=x86_64-w64-windows-gnu --sysroot=$sysroot"
export ORT_SKIP_DOWNLOAD=1
export ORT_PREFER_DYNAMIC_LINK=1
# Only public updater configuration enters OBS. Signing stays in GitHub.
unset TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD
export TAURI_UPDATER_PUBLIC_KEY=$(python3 -c 'import json; print(json.load(open(".obs/release-settings.json"))["public_key"])')
export TAURI_UPDATER_ENDPOINT=$(python3 -c 'import json; print(json.load(open(".obs/release-settings.json"))["endpoint"])')
source_commit=$(python3 -c 'import json; print(json.load(open(".obs/release-settings.json"))["commit"])')
test -n "$TAURI_UPDATER_PUBLIC_KEY"
test -n "$TAURI_UPDATER_ENDPOINT"
mkdir -p "$CARGO_HOME" "$obs/stage" "$obs/artifacts" .cargo
for dll in libgcc_s_seh-1.dll libstdc++-6.dll libwinpthread-1.dll; do
    runtime=$(find "$sysroot" /usr/lib64/gcc /usr/lib/gcc -name "$dll" -print -quit 2>/dev/null || true)
    if [ -z "$runtime" ]; then
        printf 'Missing MinGW runtime DLL: %s\n' "$dll" >&2
        exit 1
    fi
    cp "$runtime" "$obs/stage/"
done
cat > .cargo/config.toml <<EOF
[source.crates-io]
replace-with = "vendored-sources"
[source.vendored-sources]
directory = "$obs/vendor"
[net]
offline = true
EOF

# Turn Windows DLL export tables into GNU-compatible import archives.
make_import_library() {
    local dll=$1 library=$2
    python3 - "$dll" "$library.def" <<'PY'
import pathlib, re, subprocess, sys
result = subprocess.check_output(['llvm-readobj', '--coff-exports', sys.argv[1]], text=True)
names = re.findall(r'^\s+Name: (.+)$', result, flags=re.MULTILINE)
if not names: raise SystemExit('No DLL exports: ' + sys.argv[1])
pathlib.Path(sys.argv[2]).write_text('LIBRARY ' + pathlib.Path(sys.argv[1]).name + '\nEXPORTS\n' + '\n'.join(names) + '\n')
PY
    "$cross-dlltool" -d "$library.def" -D "$(basename "$dll")" -l "$library"
}

ortroot=$(find "$obs/ort" -type d -name lib -print -quit)
test -n "$ortroot"
export ORT_LIB_PATH="$ortroot"
make_import_library "$ortroot/onnxruntime.dll" "$ortroot/libonnxruntime.a"
cp "$ortroot/"*.dll "$obs/stage/"
find "$obs/ort" -name '*LICENSE*' -exec cp {} "$obs/stage/ONNX-Runtime-LICENSE" \;
webview_dll="$obs/vendor/webview2-com-sys/x64/WebView2Loader.dll"
mkdir -p "$obs/webview-imports"
make_import_library "$webview_dll" "$obs/webview-imports/libWebView2Loader.dll.a"
export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS="-L native=$obs/webview-imports"
cp "$webview_dll" "$obs/stage/"

export VULKAN_SDK="$obs/vulkan-sdk"
mkdir -p "$VULKAN_SDK/lib" "$VULKAN_SDK/include"
cp -r "$obs/Vulkan-Headers-1.4.309/include/"* "$VULKAN_SDK/include/"
vk_dll=$(find "$obs/vulkan" -path '*/x64/vulkan-1.dll' -print -quit)
test -n "$vk_dll"
cp "$vk_dll" "$obs/stage/"
find "$obs/vulkan" -name VulkanRT-License.txt -exec cp {} "$obs/stage/" \;
make_import_library "$vk_dll" "$VULKAN_SDK/lib/libvulkan-1.a"
export Vulkan_LIBRARY="$VULKAN_SDK/lib/libvulkan-1.a"
export Vulkan_INCLUDE_DIR="$VULKAN_SDK/include"
export Vulkan_GLSLC_EXECUTABLE=/usr/bin/glslc

cat > "$obs/host.cmake" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_C_COMPILER /usr/bin/gcc)
set(CMAKE_CXX_COMPILER /usr/bin/g++)
EOF
export GGML_VULKAN_SHADERS_GEN_TOOLCHAIN="$obs/host.cmake"

cat > "$obs/windows.cmake" <<EOF
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)
set(CMAKE_C_COMPILER $cross-gcc)
set(CMAKE_CXX_COMPILER $cross-g++)
set(CMAKE_RC_COMPILER $cross-windres)
set(CMAKE_FIND_ROOT_PATH $sysroot)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
set(Vulkan_LIBRARY "$Vulkan_LIBRARY" CACHE FILEPATH "Windows Vulkan import library" FORCE)
set(Vulkan_INCLUDE_DIR "$Vulkan_INCLUDE_DIR" CACHE PATH "Vulkan headers" FORCE)
set(Vulkan_GLSLC_EXECUTABLE /usr/bin/glslc CACHE FILEPATH "Host shader compiler" FORCE)
EOF
export CMAKE_TOOLCHAIN_FILE="$obs/windows.cmake"

features=(--features vulkan)
if [ "$variant" = cuda ]; then
    export CUDA_PATH="$obs/cuda-cross"
    export CUDAToolkit_ROOT="$CUDA_PATH"
    export PATH="$CUDA_PATH/bin:$PATH"
    # NVIDIA's cudart.lib includes MSVC-only static loader objects. Import the
    # same official DLL exports directly using GNU import archives.
    for library in cudart cublas cublasLt; do
        rm "$CUDA_PATH/lib/x64/lib$library.a"
        "$cross-dlltool" -d "$CUDA_PATH/lib/x64/lib$library.def" -l "$CUDA_PATH/lib/x64/lib$library.a"
    done
    export CMAKE_CUDA_COMPILER=/usr/bin/clang++
    export CMAKE_CUDA_COMPILER_TARGET=x86_64-w64-windows-gnu
    export CMAKE_CUDA_ARCHITECTURES='75-real;80-real;86-real;89;120a-real'
    export CMAKE_CUDA_RUNTIME_LIBRARY=Shared
    clang_version=$(/usr/bin/clang++ -dumpversion)
    clang_resource_dir=$(/usr/bin/clang++ -print-resource-dir)
    cat > "$obs/cuda-compat.h" <<'EOF'
#include <cuda_fp16.h>
#include <cuda_bf16.h>
#undef _MSC_VER
#undef _MSC_FULL_VER
#undef _MSVC_LANG
#ifdef __CUDA_ARCH__
#undef __MINGW_INTRIN_INLINE
#endif
#include <windows.h>
EOF
    cxx_headers=$(dirname "$(find /usr/lib64/gcc /usr/lib/gcc -path '*/x86_64-w64-mingw32/*/include/c++/climits' -print -quit 2>/dev/null || true)")
    test -f "$cxx_headers/climits"
    export CMAKE_CUDA_FLAGS="--target=x86_64-w64-windows-gnu --sysroot=$sysroot --cuda-path=$CUDA_PATH --cuda-include-ptx=sm_89 -I$CUDA_PATH/include/cccl -isystem $clang_resource_dir/include/cuda_wrappers -isystem $cxx_headers -isystem $cxx_headers/x86_64-w64-mingw32 -isystem $cxx_headers/backward -D_MSC_VER=1940 -D_MSC_FULL_VER=194033000 -D_MSVC_LANG=201703L -D__CRT__NO_INLINE -fms-extensions -include $obs/cuda-compat.h"
    cat >> "$obs/windows.cmake" <<EOF
set(CMAKE_CUDA_COMPILER /usr/bin/clang++)
set(CMAKE_CUDA_COMPILER_TARGET x86_64-w64-windows-gnu)
# CMake 3.31 probes Clang with removed SM 52/30/20 architectures. The explicit
# Final linked CUDA executable is checked for all five architectures.
set(CMAKE_CUDA_COMPILER_ID_RUN TRUE)
set(CMAKE_CUDA_COMPILER_ID Clang)
set(CMAKE_CUDA_COMPILER_VERSION "$clang_version")
set(CMAKE_CUDA_COMPILER_FRONTEND_VARIANT GNU)
set(CMAKE_CUDA_COMPILER_TOOLKIT_ROOT "$CUDA_PATH")
set(CMAKE_CUDA_COMPILER_LIBRARY_ROOT "$CUDA_PATH")
set(CMAKE_CUDA_COMPILER_TOOLKIT_VERSION 13.2.0)
set(CMAKE_CUDA_COMPILER_WORKS TRUE)
set(CMAKE_CUDA_ABI_COMPILED TRUE)
set(CMAKE_CUDA_SIZEOF_DATA_PTR 8)
set(CMAKE_CUDA_BYTE_ORDER LITTLE_ENDIAN)
set(CMAKE_CUDA_STANDARD_COMPUTED_DEFAULT 17)
set(CMAKE_CUDA_EXTENSIONS_COMPUTED_DEFAULT ON)
set(CUDAToolkit_ROOT "$CUDA_PATH")
list(APPEND CMAKE_FIND_ROOT_PATH "$CUDA_PATH")
set(CUDA_CUDART "$CUDA_PATH/lib/x64/libcudart.a" CACHE FILEPATH "Windows CUDA runtime import library" FORCE)
set(CUDA_cudart_LIBRARY "$CUDA_PATH/lib/x64/libcudart.a" CACHE FILEPATH "Windows CUDA runtime" FORCE)
set(CUDA_cuda_driver_LIBRARY "$CUDA_PATH/lib/x64/libcuda.a" CACHE FILEPATH "Windows CUDA driver" FORCE)
set(CUDA_cublas_LIBRARY "$CUDA_PATH/lib/x64/libcublas.a" CACHE FILEPATH "Windows cuBLAS" FORCE)
set(CUDA_cublasLt_LIBRARY "$CUDA_PATH/lib/x64/libcublasLt.a" CACHE FILEPATH "Windows cuBLAS Lt" FORCE)
set(CMAKE_CUDA_ARCHITECTURES "75-real;80-real;86-real;89;120a-real" CACHE STRING "Portable GPU architectures" FORCE)
set(CMAKE_CUDA_RUNTIME_LIBRARY Shared CACHE STRING "CUDA runtime linkage" FORCE)
set(CMAKE_CUDA_FLAGS "$CMAKE_CUDA_FLAGS" CACHE STRING "CUDA cross target flags" FORCE)
EOF
    features=(--features cuda)
fi

export npm_config_cache="$obs/npm-cache"
export npm_config_offline=true
npm ci --offline --ignore-scripts --no-audit --no-fund
npm run check:i18n
npm --workspace apps/desktop test
npm run build:frontend
rustc --version
cargo build --offline --locked --release --jobs "$build_jobs" --target "$target" \
    --manifest-path apps/desktop/src-tauri/Cargo.toml "${features[@]}"
cp "$CARGO_TARGET_DIR/$target/release/vrcs-desktop.exe" "$obs/stage/"
cp LICENSE THIRD_PARTY_NOTICES.md "$obs/stage/"
python3 - "$obs" <<'PY'
import pathlib, shutil, sys
obs = pathlib.Path(sys.argv[1])
licenses = obs / 'stage/third-party-licenses'
for source in (obs / 'vendor').rglob('*'):
    if source.is_file() and source.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')):
        destination = licenses / 'cargo' / source.relative_to(obs / 'vendor')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
for source in (obs / 'ort').rglob('ThirdPartyNotices.txt'):
    destination = licenses / 'ONNX-Runtime-ThirdPartyNotices.txt'
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
PY
suffix=''
if [ "$variant" = cuda ]; then suffix=-CUDA; fi
filename="VRCS-$version-windows-x64$suffix.exe"
python3 "$obs/tauri-bundle.py" "$variant" "$version"
if [ "$variant" = cuda ]; then
    python3 "$obs/check-cuda.py" "$obs/stage/vrcs-desktop.exe" "$obs/artifacts"
fi
(
    cd "$obs/artifacts"
    sha256sum "$filename" > "$filename.sha256"
)
cat > "$obs/artifacts/BUILD-INFO-$variant.txt" <<EOF
VRCS $version ($variant), compiled from source in OBS
Source commit: $source_commit
Rust target: $target
Rust: $(rustc --version)
CUDA toolkit: 13.2.0 (CUDA variant only)
Updater: enabled with pinned public key; installer is signed after OBS download
Updater endpoint: $TAURI_UPDATER_ENDPOINT
Installer: Tauri CLI 2.11.4 / NSIS 3.11, upstream installer hooks
WebView2: embedded Microsoft bootstrapper, downloads runtime when required
Prerequisites: Windows 10/11 x64, VC++ v14 x64 runtime
CUDA variant: NVIDIA CUDA 13 runtime/cuBLAS and a compatible driver
Runtime execution on Windows is not tested by this Linux build worker.
EOF
