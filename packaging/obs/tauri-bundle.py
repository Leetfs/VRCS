"""Bundle with the pinned Tauri CLI and upstream NSIS configuration/hooks."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import os

parser = argparse.ArgumentParser()
parser.add_argument('variant', choices=['standard', 'cuda'])
parser.add_argument('version')
args = parser.parse_args()
root = Path.cwd()
obs = root / '.obs'
target = 'x86_64-pc-windows-gnu'
app = root / 'apps/desktop/src-tauri'
target_root = Path(os.environ['CARGO_TARGET_DIR'])
config = json.loads((app / 'tauri.release.conf.json').read_text())
config['bundle']['createUpdaterArtifacts'] = False
config['bundle']['useLocalToolsDir'] = True
config['plugins'] = {'updater': {
    'pubkey': os.environ['TAURI_UPDATER_PUBLIC_KEY'],
    'endpoints': [os.environ['TAURI_UPDATER_ENDPOINT']],
}}
# The original upstream NSIS installerHooks, languages, install mode and
# embedBootstrapper settings are retained. Additional GNU/ORT DLLs are resources.
config['bundle']['resources'] = {
    str(p.resolve()) + ('/' if p.is_dir() else ''):
    p.name + ('/' if p.is_dir() else '')
    for p in sorted((obs / 'stage').iterdir()) if p.name != 'vrcs-desktop.exe'
}
cache = target_root / '.tauri'
cache.mkdir(parents=True, exist_ok=True)
shutil.copytree(obs / 'tauri-tools/NSIS', cache / 'NSIS', dirs_exist_ok=True)
shutil.copy2(obs / 'tauri-tools/MicrosoftEdgeWebview2Setup.exe', cache)
config_path = app / 'tauri.obs.release.conf.json'
config_path.write_text(json.dumps(config, indent=2))
env = dict(os.environ, CARGO_TARGET_DIR=str(target_root))
cli = obs / 'tauri-tools/cli/node_modules/@tauri-apps/cli/tauri.js'
features = 'vulkan' if args.variant == 'standard' else 'cuda'
subprocess.run(['node', str(cli), 'bundle', '--target', target,
                '--features', features, '--bundles', 'nsis', '--ci', '--no-sign',
                '--config', str(config_path)], cwd=app.parent, env=env, check=True)
installers = list((target_root / target / 'release/bundle/nsis').glob('*.exe'))
if len(installers) != 1:
    raise SystemExit('Expected one Tauri NSIS installer, got ' + str(installers))
suffix = '-CUDA' if args.variant == 'cuda' else ''
artifacts = obs / 'artifacts'
artifacts.mkdir(exist_ok=True)
shutil.copy2(installers[0], artifacts / f'VRCS-{args.version}-windows-x64{suffix}.exe')
shutil.copy2(target_root / target / 'release/vrcs-desktop.exe', obs / 'stage')
shutil.copy2(target_root / target / 'release/nsis/x64/installer.nsi',
             artifacts / f'installer-{args.variant}.nsi')
shutil.copy2(config_path, artifacts / f'tauri-release-{args.variant}.json')
print(f'Tauri NSIS release completed: {installers[0].stat().st_size} bytes')
