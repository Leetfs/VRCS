# VRCS is compiled from pinned source. No VRCS release executable is an input.
%define flavor @BUILD_FLAVOR@%{nil}
%if "%{flavor}" == ""
%define flavor standard
%define base_build 1
%else
%define base_build 0
%endif
%global rust_version 1.99.0
%global debug_package %{nil}
%global __os_install_post %{nil}
%global _use_internal_dependency_generator 0

Name:           vrcs-windows-%{flavor}
Version:        0.2.1
Release:        0
Summary:        VRCS Windows x64 installer compiled from source (%{flavor})
License:        AGPL-3.0-only
URL:            https://github.com/Dreaminko/VRCS
Source0:        VRCS-%{version}.tar.xz
Source1:        cargo-vendor.tar.xz
Source2:        npm-cache.tar.xz
Source3:        rustc-%{rust_version}-x86_64-unknown-linux-gnu.tar.xz
Source4:        cargo-%{rust_version}-x86_64-unknown-linux-gnu.tar.xz
Source5:        rust-std-%{rust_version}-x86_64-unknown-linux-gnu.tar.xz
Source6:        rust-std-%{rust_version}-x86_64-pc-windows-gnu.tar.xz
%if "%{flavor}" == "cuda"
Source7:        cuda-cross-13.2.0.tar.xz
%endif
Source9:        VulkanRT-1.4.309.0-Components.zip
Source10:       vulkan-headers-1.4.309.tar.gz
Source11:       onnxruntime-win-x64-1.24.4-runtime.tar.xz
Source12:       build-windows.sh
Source13:       patch-cross-build.py
Source14:       tauri-bundle.py
Source15:       SHA256SUMS.inputs
Source17:       README.md
Source22:       release-settings.json
Source18:       check-cuda.py
Source19:       tauri-tools-2.11.4.tar.xz
Source20:       nsis-v311.tar.gz
BuildRequires:  clang
BuildRequires:  cmake
BuildRequires:  gcc-c++
BuildRequires:  libclang13
BuildRequires:  llvm
BuildRequires:  mingw64-cross-binutils
BuildRequires:  mingw64-cross-gcc-c++
BuildRequires:  mingw64-headers
BuildRequires:  mingw64-libgcc_s_seh1
BuildRequires:  mingw64-libstdc++6
BuildRequires:  mingw64-libwinpthread1
BuildRequires:  mingw64-runtime
BuildRequires:  ninja
BuildRequires:  nodejs24
BuildRequires:  npm24
BuildRequires:  openssl-devel
BuildRequires:  pkgconf-pkg-config
# Tauri CLI probes its Linux tray dependency even while bundling Windows.
BuildRequires:  pkgconfig(ayatana-appindicator3-0.1)
BuildRequires:  scons
BuildRequires:  zlib-devel
BuildRequires:  python3
BuildRequires:  shaderc
BuildRequires:  unzip
BuildRequires:  xz
ExclusiveArch:  x86_64
%if %{base_build}
ExcludeArch:    x86_64
%endif
AutoReqProv:    no

%description
Cross-compiles VRCS Rust/Tauri and its frontend into a Windows x64 NSIS
installer. Both variants include CPU and Vulkan Whisper backends. The CUDA
flavor adds the Whisper CUDA backend, compiled with LLVM using CUDA 13.2
Windows headers/import libraries and Linux device-code tools.
Third-party runtime dependencies are pinned separately, as in upstream.
Only the updater public key is embedded in OBS. Private signing runs in GitHub.

%prep
# Check all supplied archives before extraction. CUDA-only inputs are verified
# only when present, so standard builds do not need the CUDA toolchain archive.
python3 - '%{_sourcedir}' '%{SOURCE15}' <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
for line in pathlib.Path(sys.argv[2]).read_text().splitlines():
    expected, name = line.split('  ', 1)
    path = root / name
    if not path.exists() and name == 'cuda-cross-13.2.0.tar.xz':
        continue
    actual = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    if actual != expected:
        raise SystemExit('Input SHA256 mismatch: ' + name)
PY
%setup -q -n VRCS-%{version}
mkdir -p .obs
tar -xJf %{SOURCE1} -C .obs
tar -xJf %{SOURCE2} -C .obs
for archive in %{SOURCE3} %{SOURCE4} %{SOURCE5} %{SOURCE6}; do
    tar -xJf "$archive" -C .obs
done
tar -xzf %{SOURCE10} -C .obs
unzip -q %{SOURCE9} -d .obs/vulkan
mkdir -p .obs/ort
tar -xJf %{SOURCE11} -C .obs/ort
%if "%{flavor}" == "cuda"
tar -xJf %{SOURCE7} -C .obs
%endif
cp %{SOURCE12} %{SOURCE13} %{SOURCE14} %{SOURCE18} %{SOURCE22} .obs/
tar -xJf %{SOURCE19} -C .obs
mkdir -p .obs/nsis-src
tar -xzf %{SOURCE20} -C .obs/nsis-src --strip-components=1
python3 .obs/patch-cross-build.py .obs/vendor

%build
bash .obs/build-windows.sh '%{flavor}' '%{version}' '%{_sourcedir}'

%check
python3 - '%{flavor}' '%{version}' <<'PY'
import pathlib, struct, sys
suffix = '-CUDA' if sys.argv[1] == 'cuda' else ''
for path in [pathlib.Path('.obs/stage/vrcs-desktop.exe'),
             pathlib.Path('.obs/artifacts/VRCS-' + sys.argv[2] + '-windows-x64' + suffix + '.exe')]:
    with path.open('rb') as f:
        if f.read(2) != b'MZ': raise SystemExit('Not a Windows executable: ' + str(path))
        f.seek(0x3c); offset = struct.unpack('<I', f.read(4))[0]
        f.seek(offset)
        if f.read(4) != b'PE\0\0': raise SystemExit('Invalid PE: ' + str(path))
        machine = struct.unpack('<H', f.read(2))[0]
        if path.name == 'vrcs-desktop.exe' and machine != 0x8664:
            raise SystemExit('VRCS application is not x64')
PY

%install
mkdir -p %{buildroot}%{_datadir}/vrcs-windows-%{flavor}
cp .obs/artifacts/* %{buildroot}%{_datadir}/vrcs-windows-%{flavor}/
# OBS collects OTHER independently of the RPM output directories.
mkdir -p %{_topdir}/OTHER
cp .obs/artifacts/* %{_topdir}/OTHER/

%files
%license LICENSE
%{_datadir}/vrcs-windows-%{flavor}/

%changelog
