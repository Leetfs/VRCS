"""Reserve a patch release and publish it only after OBS and signing succeed."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def version_tuple(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"Stable three-part version required: {value}")
    return tuple(map(int, value.split(".")))


def next_version(current, tags):
    versions = [version_tuple(current)]
    versions += [version_tuple(t) for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t)]
    major, minor, patch = max(versions)
    return f"{major}.{minor}.{patch + 1}"


def update_versions(root, version):
    for name in ["apps/desktop/src-tauri/tauri.conf.json", "apps/desktop/package.json", "package-lock.json"]:
        path = root / name
        doc = json.loads(path.read_text())
        if name == "package-lock.json":
            if "version" in doc:
                doc["version"] = version
            for key in ["", "apps/desktop"]:
                if "version" in doc.get("packages", {}).get(key, {}):
                    doc["packages"][key]["version"] = version
        else:
            doc["version"] = version
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    for name in ["core/Cargo.toml", "apps/desktop/src-tauri/Cargo.toml"]:
        path = root / name
        text, count = re.subn(r'(?m)^version = "[^"\n]+"$', f'version = "{version}"', path.read_text(), count=1)
        if count != 1:
            raise ValueError(f"Package version not found: {name}")
        path.write_text(text)
    for name in ["core/Cargo.lock", "apps/desktop/src-tauri/Cargo.lock"]:
        path = root / name
        text = re.sub(r'(\[\[package\]\]\nname = "vrcs-(?:core|desktop)"\nversion = ")[^"]+("\n)',
                      lambda m: m[1] + version + m[2], path.read_text())
        path.write_text(text)


def markdown(value):
    return re.sub(r"([\\`*{}\[\]<>#|])", r"\\\1", value.replace("\n", " "))


def release_notes(repository, version, previous, source):
    revision = f"{previous}..{source}" if previous else source
    arguments = ["git", "log", "--reverse", "--format=%H%x00%an%x00%s", revision]
    if not previous:
        arguments.insert(2, "-1")
    commits = []
    authors = set()
    for line in run(*arguments).splitlines():
        sha, author, subject = line.split("\0", 2)
        if subject.startswith("chore(release):"):
            continue
        authors.add(author)
        commits.append(f"- [{sha[:8]}](https://github.com/{repository}/commit/{sha}) — {markdown(subject)}（作者：{markdown(author)}）")
    return (f"# VRCS {version}\n\n"
            "由 GitHub Actions 触发 OBS，从源码构建 Windows x64 普通版与 CUDA 版。\n\n"
            "- 普通版：CPU/Vulkan Whisper；CUDA 版额外支持 NVIDIA CUDA 加速。\n"
            "- NSIS 3.11 / Tauri 原始模板与安装钩子；嵌入 WebView2 引导程序，缺少运行时时联网安装。\n"
            "- CUDA 75/80/86/89/120a cubin，PTX 89 回退。CUDA 13 runtime/cuBLAS、兼容驱动需另行安装。\n"
            "- Tauri 更新签名 `.sig` 和分版本 `latest.json` 已发布；这不是 Authenticode 签名。\n"
            "- LLVM/MinGW 交叉编译；前端及产物检查通过，Windows/GPU 实机行为尚未验证。\n\n"
            f"构建源码：[{source}](https://github.com/{repository}/commit/{source})\n\n"
            "## 作者\n\n" + (", ".join(markdown(a) for a in sorted(authors)) or "无新增用户提交") +
            "\n\n## 提交\n\n" + ("\n".join(commits) or "沿用此前发布提交。") + "\n")


def reserve(root, output, repository, branch):
    output.mkdir(parents=True, exist_ok=True)
    state_path = root / ".release-state.json"
    head = run("git", "rev-parse", "HEAD")
    message = run("git", "log", "-1", "--format=%s")
    if state_path.exists() and message.startswith("chore(release):"):
        state = json.loads(state_path.read_text())
        if state["repository"] != repository:
            raise ValueError("Release state belongs to another repository; make a new source commit first")
        print(f"Resuming reserved release {state['version']}")
    else:
        current = json.loads((root / "apps/desktop/src-tauri/tauri.conf.json").read_text())["version"]
        tags = run("git", "tag", "--merged", head).splitlines()
        version = next_version(current, tags)
        previous = max((t for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t)), key=version_tuple, default=None)
        # A release can have failed after reserving its version. Use the most recent
        # published GitHub release as the changelog boundary when one exists.
        releases = json.loads(run("gh", "api", f"repos/{repository}/releases?per_page=100"))
        published = [r["tag_name"] for r in releases if not r["draft"] and not r["prerelease"]
                     and r["tag_name"] in tags and re.fullmatch(r"\d+\.\d+\.\d+", r["tag_name"])]
        if published:
            previous = max(published, key=version_tuple)
        state = {"repository": repository, "version": version, "source_commit": head, "previous_tag": previous}
        update_versions(root, version)
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", ".release-state.json", "core/Cargo.toml", "core/Cargo.lock",
                        "apps/desktop/src-tauri/Cargo.toml", "apps/desktop/src-tauri/Cargo.lock",
                        "apps/desktop/src-tauri/tauri.conf.json", "apps/desktop/package.json", "package-lock.json"], check=True)
        subprocess.run(["git", "commit", "-m", f"chore(release): {version} [skip ci]"], check=True)
        # Never force-push over a user commit arriving while versions are reserved.
        # Keep this workflow tree reachable from a branch until its tag is
        # published. GitHub otherwise rejects GITHUB_TOKEN tag pushes if main
        # receives a workflow change during the OBS build.
        subprocess.run(["git", "push", "--atomic", "origin", f"HEAD:refs/heads/{branch}",
                        f"HEAD:refs/heads/obs-release/{version}"], check=True)
        head = run("git", "rev-parse", "HEAD")
    snapshot = f"refs/heads/obs-release/{state['version']}"
    remote = run("git", "ls-remote", "origin", snapshot)
    if remote and remote.split()[0] != head:
        raise ValueError("Release snapshot branch points at another commit")
    if not remote:
        # Resume releases reserved before snapshot branches were introduced.
        subprocess.run(["git", "push", "origin", f"{head}:{snapshot}"], check=True)
    state["version_commit"] = head
    state["published_at"] = datetime.now(timezone.utc).isoformat()
    (output / "release.json").write_text(json.dumps(state, indent=2) + "\n")
    (output / "notes.md").write_text(release_notes(repository, state["version"], state["previous_tag"], state["source_commit"]))
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"version={state['version']}\ncommit={head}\n")
    print(f"Reserved {state['version']} at {head}")


def publish(metadata, artifacts, notes):
    doc = json.loads(metadata.read_text())
    version, repository, commit = doc["version"], doc["repository"], doc["version_commit"]
    existing = subprocess.run(["git", "rev-parse", "--verify", f"refs/tags/{version}^{{commit}}"], capture_output=True, text=True)
    if existing.returncode == 0:
        if existing.stdout.strip() != commit:
            raise ValueError(f"Existing tag {version} points at another commit")
    else:
        subprocess.run(["git", "tag", "-a", version, commit, "-m", f"VRCS {version}"], check=True)
    subprocess.run(["git", "push", "origin", f"refs/tags/{version}"], check=True)
    view = subprocess.run(["gh", "release", "view", version, "--repo", repository, "--json", "isDraft"], capture_output=True, text=True)
    if view.returncode:
        subprocess.run(["gh", "release", "create", version, "--repo", repository, "--verify-tag", "--draft",
                        "--title", f"VRCS {version}", "--notes-file", str(notes)], check=True)
    else:
        if not json.loads(view.stdout)["isDraft"]:
            print(f"Release {version} is already published; leaving its signed assets unchanged")
            return
        subprocess.run(["gh", "release", "edit", version, "--repo", repository, "--notes-file", str(notes)], check=True)
    assets = [p for p in artifacts.iterdir() if p.is_file() and
              (p.name.startswith(f"VRCS-{version}-") or p.name in ["latest.json", "release-provenance.json"])]
    required = {f"VRCS-{version}-windows-x64{s}.exe{extension}" for s in ["", "-CUDA"] for extension in ["", ".sig", ".sha256"]}
    if not required <= {p.name for p in assets}:
        raise ValueError("Incomplete installer/signature/checksum set")
    subprocess.run(["gh", "release", "upload", version, "--repo", repository, "--clobber", *map(str, sorted(assets))], check=True)
    subprocess.run(["gh", "release", "edit", version, "--repo", repository, "--draft=false", "--latest"], check=True)
    cleanup = subprocess.run(["gh", "api", "--method", "DELETE",
                              f"repos/{repository}/git/refs/heads/obs-release/{version}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if cleanup.returncode:
        print("Release published; temporary snapshot branch cleanup can be retried")
    print(f"Published https://github.com/{repository}/releases/tag/{version}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["reserve", "publish"])
    parser.add_argument("--metadata", type=Path, default=Path(".release/release.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("release-artifacts"))
    args = parser.parse_args()
    if args.command == "reserve":
        reserve(Path.cwd(), args.metadata.parent, os.environ["GITHUB_REPOSITORY"], os.environ["RELEASE_BRANCH"])
    else:
        publish(args.metadata, args.artifacts, args.metadata.parent / "notes.md")
