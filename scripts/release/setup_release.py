"""One-time maintainer setup. Keys and passwords never enter the repository."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess


def secret(repository, name, value):
    subprocess.run(["gh", "secret", "set", name, "--repo", repository], input=value,
                   text=True, check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--obs-project", required=True)
    parser.add_argument("--obs-package", default="vrcs-windows-actions")
    parser.add_argument("--obs-repository", default="windows_x64")
    parser.add_argument("--key-dir", type=Path, default=Path(".release/signing"))
    parser.add_argument("--cli", type=Path, default=Path(".release/signing-cli/node_modules/@tauri-apps/cli/tauri.js"))
    args = parser.parse_args()
    names = json.loads(subprocess.check_output(["gh", "secret", "list", "--repo", args.repo, "--json", "name"], text=True))
    if any(s["name"].startswith("TAURI_") for s in names):
        raise SystemExit("Existing Tauri signing Secrets found. Preserve them; configure OBS Secrets separately. Never rotate an existing updater key automatically.")
    username = os.getenv("OBS_USERNAME") or input("OBS username: ").strip()
    password = os.getenv("OBS_PASSWORD") or getpass.getpass("OBS password: ")
    if not username or not password:
        raise SystemExit("OBS credentials are required")
    args.key_dir.mkdir(parents=True, exist_ok=True)
    args.key_dir.chmod(0o700)
    key = args.key_dir / "updater.key"
    key_password = args.key_dir / "updater-password.txt"
    if key.exists() or key_password.exists():
        raise SystemExit("Local signing key already exists; do not overwrite it. Complete setup using this backup.")
    key_password.write_text(secrets.token_urlsafe(40))
    key_password.chmod(0o600)
    env = dict(os.environ, CI="true")
    with (args.key_dir / "generate.log").open("w") as log:
        subprocess.run(["node", str(args.cli.resolve()), "signer", "generate", "--ci", "--write-keys", str(key.resolve()),
                        "--password", key_password.read_text()], check=True, env=env, stdout=log, stderr=log)
    for file in args.key_dir.iterdir():
        file.chmod(0o600)
    secret(args.repo, "OBS_USERNAME", username)
    secret(args.repo, "OBS_PASSWORD", password)
    secret(args.repo, "TAURI_SIGNING_PRIVATE_KEY", key.read_text())
    secret(args.repo, "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", key_password.read_text())
    secret(args.repo, "TAURI_UPDATER_PUBLIC_KEY", Path(str(key) + ".pub").read_text().strip())
    for name, value in {"OBS_PROJECT": args.obs_project, "OBS_PACKAGE": args.obs_package,
                        "OBS_REPOSITORY": args.obs_repository}.items():
        subprocess.run(["gh", "variable", "set", name, "--repo", args.repo, "--body", value], check=True)
    print(f"Configured OBS and signing Secrets for {args.repo}")
    print(f"Encrypted key backup and separate password saved in {args.key_dir.resolve()}; keep both private and back them up.")


if __name__ == "__main__":
    main()
