"""Sign OBS installers with Tauri and verify each signature using minisign."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def decoded(value, name):
    try:
        text = base64.b64decode(value.strip(), validate=True)
    except ValueError as error:
        raise ValueError(f"Invalid base64 {name}") from error
    if not text.startswith(b"untrusted comment:"):
        raise ValueError(f"Invalid Tauri/minisign {name}")
    return text


def verify(file, signature, public_key):
    with tempfile.TemporaryDirectory(prefix="vrcs-verify-") as directory:
        public = Path(directory) / "public.key"
        sig = Path(directory) / "file.minisig"
        public.write_bytes(decoded(public_key, "public key"))
        sig.write_bytes(decoded(signature, "signature"))
        result = subprocess.run(["minisign", "-Vm", str(file), "-p", str(public), "-x", str(sig)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0


def sign(file, cli):
    if not os.getenv("TAURI_SIGNING_PRIVATE_KEY"):
        raise ValueError("TAURI_SIGNING_PRIVATE_KEY is missing")
    result = subprocess.run(["node", str(cli), "signer", "sign", str(file)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(f"Tauri signing failed with exit code {result.returncode}; check signing Secrets")
    signature = Path(str(file) + ".sig").read_text().strip()
    if not verify(file, signature, os.environ["TAURI_UPDATER_PUBLIC_KEY"]):
        raise ValueError(f"Signature/public key verification failed: {file.name}")
    return signature


def preflight(cli):
    with tempfile.TemporaryDirectory(prefix="vrcs-signing-") as directory:
        file = Path(directory) / "fixture.txt"
        file.write_bytes(b"VRCS updater key validation\n")
        signature = sign(file, cli)
        file.write_bytes(b"tampered\n")
        if verify(file, signature, os.environ["TAURI_UPDATER_PUBLIC_KEY"]):
            raise ValueError("Verifier accepted modified bytes")
    print("Signing key matches public key; modified payload is rejected")


def sign_release(metadata, artifacts, cli):
    doc = json.loads(metadata.read_text())
    platforms = {}
    checksums = {}
    for flavor, suffix in [("standard", ""), ("cuda", "-CUDA")]:
        file = artifacts / f"VRCS-{doc['version']}-windows-x64{suffix}.exe"
        checksum = hashlib.sha256(file.read_bytes()).hexdigest()
        if checksum != Path(str(file) + ".sha256").read_text().split()[0]:
            raise ValueError(f"Checksum mismatch before signing: {file.name}")
        signature = sign(file, cli)
        platforms[f"windows-x86_64-{flavor}"] = {
            "url": f"https://github.com/{doc['repository']}/releases/download/{doc['version']}/{file.name}",
            "signature": signature,
        }
        checksums[file.name] = checksum
        print(f"Signed and independently verified {file.name}")
    latest = {"version": doc["version"], "pub_date": doc["published_at"],
              "notes": (metadata.parent / "notes.md").read_text(), "platforms": platforms}
    (artifacts / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n")
    provenance = artifacts / "release-provenance.json"
    info = json.loads(provenance.read_text())
    info["sha256"] = checksums
    info["updater_signatures_verified"] = True
    provenance.write_text(json.dumps(info, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "release"])
    parser.add_argument("--cli", type=Path, default=Path(".release/signing-cli/node_modules/@tauri-apps/cli/tauri.js"))
    parser.add_argument("--metadata", type=Path, default=Path(".release/release.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("release-artifacts"))
    args = parser.parse_args()
    if args.command == "preflight":
        preflight(args.cli)
    else:
        sign_release(args.metadata, args.artifacts, args.cli)
