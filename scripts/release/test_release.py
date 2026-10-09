"""Small tests for versioning, source-cache invalidation and release notes."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from obs_release import fingerprint
from release import next_version, release_notes, update_versions


class ReleaseTests(unittest.TestCase):
    def test_patch_version_respects_existing_tags(self):
        self.assertEqual(next_version("0.2.1", ["0.2.9", "0.1.99", "preview"]), "0.2.10")
        self.assertEqual(next_version("0.3.0", ["0.2.9"]), "0.3.1")

    def test_unstable_version_cannot_accidentally_publish(self):
        with self.assertRaises(ValueError):
            next_version("0.2.1-beta", [])

    def test_version_bump_keeps_all_manifests_in_sync_and_cache_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["core", "apps/desktop/src-tauri"]:
                folder = root / name
                folder.mkdir(parents=True)
                package = "vrcs-core" if name == "core" else "vrcs-desktop"
                (folder / "Cargo.toml").write_text(f'[package]\nname = "{package}"\nversion = "0.2.1"\n')
                (folder / "Cargo.lock").write_text(f'version = 4\n\n[[package]]\nname = "{package}"\nversion = "0.2.1"\n\n[[package]]\nname = "external"\nversion = "1.0.0"\nsource = "registry+https://example.com"\n')
            for name in ["apps/desktop/package.json", "apps/desktop/src-tauri/tauri.conf.json"]:
                (root / name).write_text('{"version": "0.2.1"}')
            lock = {"packages": {"apps/desktop": {"version": "0.2.1"}, "node_modules/dependency": {"version": "1.0.0"}}}
            (root / "package-lock.json").write_text(json.dumps(lock))
            files = [root / "core/Cargo.lock", root / "apps/desktop/src-tauri/Cargo.lock", root / "package-lock.json"]
            before = [fingerprint(p) for p in files]
            update_versions(root, "0.2.2")
            self.assertEqual(before, [fingerprint(p) for p in files])
            for name in ["core/Cargo.toml", "core/Cargo.lock", "apps/desktop/src-tauri/Cargo.toml", "apps/desktop/src-tauri/Cargo.lock"]:
                self.assertIn('version = "0.2.2"', (root / name).read_text())
            self.assertEqual(json.loads((root / "package-lock.json").read_text())["packages"]["apps/desktop"]["version"], "0.2.2")
            files[0].write_text(files[0].read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
            self.assertNotEqual(before[0], fingerprint(files[0]))

    def test_notes_include_author_commit_and_escape_message(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "-c", "user.name=Example Author", "-c", "user.email=test@example.com",
                            "commit", "--allow-empty", "-qm", "Fix [update] <link>"], check=True)
            import os
            previous = Path.cwd()
            try:
                os.chdir(directory)
                sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
                notes = release_notes("Leetfs/VRCS", "0.2.2", None, sha)
            finally:
                os.chdir(previous)
            self.assertIn("Example Author", notes)
            self.assertIn(f"https://github.com/Leetfs/VRCS/commit/{sha}", notes)
            self.assertIn(r"Fix \[update\] \<link\>", notes)


if __name__ == "__main__":
    unittest.main()
