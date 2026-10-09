"""Submit exact Git source to OBS, wait for both flavors, and verify downloads."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib
import urllib.error
import xml.etree.ElementTree as ET

from obs_client import Client, path, query

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging/obs"


def fingerprint(file):
    doc = json.loads(file.read_text()) if file.suffix == ".json" else tomllib.loads(file.read_text())
    if file.suffix == ".json":
        doc.pop("version", None)
        for key in ["", "apps/desktop"]:
            if key in doc["packages"]:
                doc["packages"][key].pop("version", None)
    else:
        for package in doc["package"]:
            if package["name"] in ["vrcs-core", "vrcs-desktop"] and "source" not in package:
                package.pop("version", None)
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def preflight(client):
    public_key = os.getenv("TAURI_UPDATER_PUBLIC_KEY", "").strip()
    if not public_key:
        raise ValueError("TAURI_UPDATER_PUBLIC_KEY is missing")
    client.request(path("person", os.environ["OBS_USERNAME"]))
    project = os.environ["OBS_PROJECT"]
    # Projects/repositories are deliberately set up by the administrator once.
    # A release must not rewrite an existing project's global build settings.
    meta = ET.fromstring(client.request(path("source", project, "_meta")))
    repositories = {r.get("name") for r in meta.findall("repository")}
    if os.environ["OBS_REPOSITORY"] not in repositories:
        raise ValueError("Configured OBS repository is absent from the project")
    print("OBS credentials, project, repository and updater public key are configured")


def source_archive(metadata, destination):
    version, commit = metadata["version"], metadata["version_commit"]
    archive = destination / f"VRCS-{version}.tar.xz"
    with archive.open("wb") as output:
        process = subprocess.Popen(["git", "archive", "--format=tar", f"--prefix=VRCS-{version}/", commit], stdout=subprocess.PIPE)
        try:
            subprocess.run(["xz", "-T0", "-3"], stdin=process.stdout, stdout=output, check=True)
        finally:
            process.stdout.close()
        if process.wait():
            raise RuntimeError("git archive failed")
    return archive


def regenerate_dependencies(destination, changed):
    work = destination.parent / "dependency-work"
    work.mkdir(exist_ok=True)
    os.environ["VRCS_INPUT_WORKDIR"] = str(work.resolve())
    spec = importlib.util.spec_from_file_location("dependencies", PACKAGING / "prepare_dependencies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not (work / "VRCS").exists():
        (work / "VRCS").symlink_to(ROOT, target_is_directory=True)
    result = []
    if any(name.endswith("Cargo.lock") for name in changed):
        subprocess.run(["cargo", "vendor", "--locked", "--manifest-path", str(ROOT / "apps/desktop/src-tauri/Cargo.toml"),
                        "--sync", str(ROOT / "core/Cargo.toml"), str(work / "vendor")], check=True, stdout=subprocess.DEVNULL)
        module.archive("cargo-vendor.tar.xz", work / "vendor", "vendor")
        result.append("cargo-vendor.tar.xz")
    if "package-lock.json" in changed:
        module.npm_sources()
        result.append("npm-cache.tar.xz")
    for name in result:
        shutil.copyfile(module.SOURCES / name, destination / name)
    return result


def prepare(client, metadata, destination):
    destination.mkdir(parents=True, exist_ok=True)
    cache = json.loads((PACKAGING / "input-cache.json").read_text())
    baseline_path = path("source", cache["project"], cache["package"])
    baseline = ET.fromstring(client.request(baseline_path + query(rev=cache["revision"])))
    entries = {e.get("name"): e.get("md5") for e in baseline.findall("entry")}
    pins = {}
    for line in (PACKAGING / "SHA256SUMS.inputs").read_text().splitlines():
        sha, name = line.split("  ", 1)
        # Source archive and dependency archives are handled separately below.
        if name.startswith("VRCS-"):
            continue
        if name not in entries:
            raise ValueError(f"Pinned OBS input absent: {name}")
        pins[name] = sha
    changed = [name for name, digest in cache["lock_fingerprints"].items() if fingerprint(ROOT / name) != digest]
    rebuilt = regenerate_dependencies(destination, changed) if changed else []
    archive = source_archive(metadata, destination)
    pins[archive.name] = hashlib.sha256(archive.read_bytes()).hexdigest()
    spec = (PACKAGING / "vrcs-windows.spec").read_text()
    spec = re.sub(r"(?m)^Version:\s+\S+", "Version:        " + metadata["version"], spec)
    spec = re.sub(r"(?m)^URL:\s+\S+", "URL:            https://github.com/" + metadata["repository"], spec)
    (destination / "vrcs-windows.spec").write_text(spec)
    for name in ["build-windows.sh", "patch-cross-build.py", "tauri-bundle.py", "check-cuda.py", "_multibuild", "_constraints", "README.md"]:
        shutil.copyfile(PACKAGING / name, destination / name)
    settings = {"commit": metadata["version_commit"], "repository": metadata["repository"],
                "endpoint": f"https://github.com/{metadata['repository']}/releases/latest/download/latest.json",
                "public_key": os.environ["TAURI_UPDATER_PUBLIC_KEY"].strip()}
    (destination / "release-settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    for name in rebuilt:
        pins[name] = hashlib.sha256((destination / name).read_bytes()).hexdigest()
    (destination / "SHA256SUMS.inputs").write_text("".join(f"{sha}  {name}\n" for name, sha in sorted(pins.items())))
    # All pinned third-party inputs are copied server-side from the immutable
    # successful OBS revision. Changed dependencies are prepared from lockfiles.
    static = {name: entries[name] for name in pins if not (destination / name).exists()}
    return cache, static


def submit(client, metadata, sources):
    cache, static = prepare(client, metadata, sources)
    project, package = os.environ["OBS_PROJECT"], os.environ["OBS_PACKAGE"]
    target = path("source", project, package)
    package_meta = ET.Element("package", name=package, project=project)
    ET.SubElement(package_meta, "title").text = "VRCS GitHub Actions source releases"
    ET.SubElement(package_meta, "description").text = "Source builds; public updater configuration only. Private keys stay in GitHub."
    # Avoid scheduling the old baseline while files are being replaced.
    build = ET.SubElement(package_meta, "build")
    ET.SubElement(build, "disable")
    client.request(target + "/_meta", "PUT", ET.tostring(package_meta))
    client.request(target + query(cmd="copy", oproject=cache["project"], opackage=cache["package"], orev=cache["revision"]), "POST", b"")
    directory = ET.Element("directory")
    for name, md5 in sorted(static.items()):
        ET.SubElement(directory, "entry", name=name, md5=md5)
    for file in sorted(sources.iterdir()):
        if not file.is_file():
            continue
        data = file.read_bytes()
        client.request(target + "/" + file.name + query(rev="upload"), "PUT", data)
        ET.SubElement(directory, "entry", name=file.name, md5=hashlib.md5(data).hexdigest())
    comment = f"GitHub {metadata['repository']} {metadata['version']} {metadata['version_commit']}"
    result = ET.fromstring(client.request(target + query(cmd="commitfilelist", comment=comment), "POST", ET.tostring(directory)))
    if result.tag != "directory" or not result.get("srcmd5"):
        raise RuntimeError("OBS commitfilelist did not complete")
    package_meta.remove(build)
    client.request(target + "/_meta", "PUT", ET.tostring(package_meta))
    revision = {"project": project, "package": package, "repository": os.environ["OBS_REPOSITORY"],
                "arch": "x86_64", "revision": result.get("rev"), "srcmd5": result.get("srcmd5")}
    (sources.parent / "obs-revision.json").write_text(json.dumps(revision, indent=2) + "\n")
    print(f"Submitted OBS revision {revision['revision']} / {revision['srcmd5']}", flush=True)
    for flavor in ["standard", "cuda"]:
        result = ET.fromstring(client.request(path("build", project) + query(
            cmd="rebuild", package=package + ":" + flavor,
            repository=revision["repository"], arch=revision["arch"]), "POST", b""))
        if result.get("code") != "ok":
            raise RuntimeError(f"OBS did not accept {flavor} rebuild")
        print(f"Explicitly triggered OBS {package}:{flavor}", flush=True)
    return revision


def wait(client, revision, logs, timeout):
    logs.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    last_status = {}

    def report(flavor, message):
        if last_status.get(flavor) != message:
            print(f"OBS {flavor}: {message}", flush=True)
            last_status[flavor] = message

    while time.monotonic() < deadline:
        results = ET.fromstring(client.request(path("build", revision["project"], "_result") +
                                               query(repository=revision["repository"], arch=revision["arch"])))
        statuses = {s.get("package"): s for s in results.findall(".//status")}
        passed = []
        for flavor in ["standard", "cuda"]:
            name = revision["package"] + ":" + flavor
            state = statuses.get(name)
            if state is None:
                report(flavor, "Waiting for scheduling")
                passed.append(False)
                continue
            code = state.get("code")
            if code == "finished":
                code = state.findtext("details") or code
            base = path("build", revision["project"], revision["repository"], revision["arch"], name)
            if code in ["broken", "unresolvable", "disabled", "excluded"]:
                # The result can retain pre-upload flags for several minutes.
                # Fail only on an error from build information for our source,
                # rather than interpreting an old disabled status as a failure.
                info = ET.fromstring(client.request(base + "/_buildinfo"))
                matching_info = revision["srcmd5"] in [info.findtext("srcmd5"), info.findtext("verifymd5")]
                if matching_info and info.find("error") is not None:
                    raise RuntimeError(f"OBS {flavor}: {info.findtext('error')}")
                report(flavor, f"Waiting for scheduler refresh ({code})")
                passed.append(False)
                continue
            try:
                data = client.request(base + "/_log" + query(nostream=1, start=0)).decode(errors="replace")
            except RuntimeError as error:
                if isinstance(error.__cause__, urllib.error.HTTPError) and error.__cause__.code == 404:
                    report(flavor, f"{code.capitalize()}: waiting for first build log")
                    passed.append(False)
                    continue
                raise
            (logs / f"build-{flavor}.log").write_text(data)
            matching = revision["srcmd5"] in data[:1200]
            report(flavor, code.capitalize() + ("" if matching else ": waiting for current source revision"))
            if matching and code in ["failed", "broken", "unresolvable"]:
                raise RuntimeError(f"OBS {flavor} failed; see build log artifact")
            passed.append(matching and code == "succeeded")
        if all(passed):
            return
        time.sleep(30)
    raise TimeoutError("OBS did not finish before the release timeout")


def restore(client, run_id, output):
    if not re.fullmatch(r"\d+", run_id):
        raise ValueError("A numeric GitHub Actions run ID is required")
    repository = os.environ["GITHUB_REPOSITORY"]
    recovery = output / "recovery"
    subprocess.run(["gh", "run", "download", run_id, "--repo", repository,
                    "--name", "obs-release-" + run_id, "--dir", str(recovery)], check=True)
    saved = recovery / ".release"
    metadata = json.loads((saved / "release.json").read_text())
    revision = json.loads((saved / "obs-revision.json").read_text())
    if metadata["repository"] != repository:
        raise ValueError("Recovery metadata belongs to another repository")
    from release import version_tuple
    version_tuple(metadata["version"])
    commit = metadata["version_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid recovery commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/" + os.environ["RELEASE_BRANCH"]], check=True)
    state = json.loads(subprocess.check_output(["git", "show", commit + ":.release-state.json"], text=True))
    if any(state[k] != metadata[k] for k in ["repository", "version", "source_commit", "previous_tag"]):
        raise ValueError("Recovery metadata does not match the reserved source commit")
    expected = {"project": os.environ["OBS_PROJECT"], "package": os.environ["OBS_PACKAGE"],
                "repository": os.environ["OBS_REPOSITORY"], "arch": "x86_64"}
    if any(revision[k] != v for k, v in expected.items()):
        raise ValueError("Recovery OBS target differs from this workflow")
    target = path("source", revision["project"], revision["package"])
    current = ET.fromstring(client.request(target))
    pinned = ET.fromstring(client.request(target + query(rev=revision["revision"])))
    if current.get("srcmd5") != revision["srcmd5"] or pinned.get("srcmd5") != revision["srcmd5"]:
        raise ValueError("OBS source changed; cannot recover the previous build")
    settings = json.loads(client.request(target + "/release-settings.json" + query(rev=revision["revision"])))
    endpoint = f"https://github.com/{repository}/releases/latest/download/latest.json"
    if (settings["commit"] != commit or settings["repository"] != repository or settings["endpoint"] != endpoint
            or settings["public_key"] != os.environ["TAURI_UPDATER_PUBLIC_KEY"].strip()):
        raise ValueError("Recovery source/signing key mismatch")
    sources = output / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    name = "VRCS-" + metadata["version"] + ".tar.xz"
    data = client.request(target + "/" + name + query(rev=revision["revision"]))
    sums = client.request(target + "/SHA256SUMS.inputs" + query(rev=revision["revision"])).decode()
    pins = dict((line.split("  ", 1)[1], line.split("  ", 1)[0]) for line in sums.splitlines())
    if hashlib.sha256(data).hexdigest() != pins[name]:
        raise ValueError("Recovery source archive checksum mismatch")
    (sources / name).write_bytes(data)
    for name in ["release.json", "obs-revision.json", "notes.md"]:
        shutil.copyfile(saved / name, output / name)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"version={metadata['version']}\ncommit={commit}\n")
    print(f"Recovered release {metadata['version']} at OBS revision {revision['revision']}; no source resubmission")


def download(client, revision, metadata, artifacts):
    artifacts.mkdir(parents=True, exist_ok=True)
    for flavor, suffix in [("standard", ""), ("cuda", "-CUDA")]:
        base = path("build", revision["project"], revision["repository"], revision["arch"], revision["package"] + ":" + flavor)
        names = {e.get("filename") for e in ET.fromstring(client.request(base)).findall("binary")}
        required = [f"VRCS-{metadata['version']}-windows-x64{suffix}.exe", f"VRCS-{metadata['version']}-windows-x64{suffix}.exe.sha256",
                    f"BUILD-INFO-{flavor}.txt", f"installer-{flavor}.nsi", f"tauri-release-{flavor}.json"]
        if flavor == "cuda":
            required += ["cuda-architectures.txt", "cuda-ptx.txt"]
        if not set(required) <= names:
            raise ValueError(f"OBS {flavor} missing expected outputs")
        for name in required:
            (artifacts / name).write_bytes(client.request(base + "/" + name))
        exe = artifacts / required[0]
        checksum = (artifacts / required[1]).read_text().split()[0]
        if hashlib.sha256(exe.read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Checksum mismatch: {exe.name}")
        info = (artifacts / f"BUILD-INFO-{flavor}.txt").read_text()
        if metadata["version_commit"] not in info or "Updater: enabled" not in info:
            raise ValueError("OBS artifact commit/updater provenance mismatch")
        config = json.loads((artifacts / f"tauri-release-{flavor}.json").read_text())
        if config["plugins"]["updater"]["pubkey"] != os.environ["TAURI_UPDATER_PUBLIC_KEY"].strip():
            raise ValueError("Installed updater public key mismatch")
        print(f"Verified {exe.name}: {exe.stat().st_size:,} bytes", flush=True)
    source = artifacts.parent / ".release/sources" / f"VRCS-{metadata['version']}.tar.xz"
    # The actual source archive submitted to OBS accompanies the public release.
    shutil.copyfile(source, artifacts / f"VRCS-{metadata['version']}-source.tar.xz")
    (artifacts / "release-provenance.json").write_text(json.dumps({**metadata, "obs": revision,
                      "updater_public_key": os.environ["TAURI_UPDATER_PUBLIC_KEY"].strip()}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "build", "restore"])
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--metadata", type=Path, default=Path(".release/release.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("release-artifacts"))
    parser.add_argument("--timeout", type=int, default=10800)
    args = parser.parse_args()
    client = Client()
    preflight(client)
    if args.command == "restore":
        restore(client, args.run_id or "", args.metadata.parent)
    if args.command == "build":
        metadata = json.loads(args.metadata.read_text())
        revision = json.loads((args.metadata.parent / "obs-revision.json").read_text()) if args.resume else submit(
            client, metadata, args.metadata.parent / "sources")
        wait(client, revision, args.metadata.parent / "logs", args.timeout)
        download(client, revision, metadata, args.artifacts)
