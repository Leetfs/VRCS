"""Prepare pinned, offline OBS inputs; never downloads VRCS release EXEs."""
import concurrent.futures as futures
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request
import urllib.parse
import zipfile

ROOT = Path(os.environ.get('VRCS_INPUT_WORKDIR', 'vrcs-inputs')).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
SOURCES = ROOT / 'obs-sources'
SOURCES.mkdir(exist_ok=True)
CACHE = ROOT / 'downloads'
CACHE.mkdir(exist_ok=True)
PROVENANCE = {}

def fetch(url, checksum=None, filename=None):
    path = CACHE / (filename or urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1])
    if not path.exists():
        partial = path.with_suffix(path.suffix + '.part')
        for attempt in range(8):
            result = subprocess.run(['curl','--fail','--location','--silent','--show-error',
                                     '--connect-timeout','20','--max-time','90',
                                     '--continue-at','-','--output',str(partial),url])
            if result.returncode == 0: break
        else: raise RuntimeError('Download failed after resumable retries: '+url)
        partial.rename(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if checksum and digest != checksum:
        raise RuntimeError('Checksum mismatch: ' + url)
    PROVENANCE[path.name] = {'url': url, 'sha256': digest}
    print('Verified', path.name, path.stat().st_size, flush=True)
    return path

def unpack(path, destination, single_root=True):
    destination.mkdir(parents=True, exist_ok=True)
    if path.suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            archive.extractall(destination)
    else:
        with tarfile.open(path) as archive:
            archive.extractall(destination, filter='data')
    children = list(destination.iterdir())
    return children[0] if single_root and len(children) == 1 and children[0].is_dir() else destination

def archive(name, source, arcname):
    with tarfile.open(SOURCES / name, 'w:xz', preset=3) as out:
        out.add(source, arcname=arcname)
    print('Packed', name, (SOURCES/name).stat().st_size, flush=True)

def rust_toolchain():
    url = 'https://static.rust-lang.org/dist/'
    manifest = fetch(url + 'channel-rust-1.99.0.toml')
    doc = tomllib.loads(manifest.read_text())
    tasks = [('rustc','x86_64-unknown-linux-gnu'),('cargo','x86_64-unknown-linux-gnu'),
             ('rust-std','x86_64-unknown-linux-gnu'),('rust-std','x86_64-pc-windows-gnu')]
    for name, target in tasks:
        info = doc['pkg'][name]['target'][target]
        path = fetch(info['xz_url'], info['xz_hash'])
        shutil.copy2(path, SOURCES/path.name)

def nvidia_toolkit():
    base = 'https://developer.download.nvidia.com/compute/cuda/redist/'
    manifest = fetch(base+'redistrib_13.2.0.json')
    doc = json.loads(manifest.read_text())
    dest = ROOT/'cuda-cross'
    for component, target in [('cuda_nvcc','linux-x86_64'),('cuda_cuobjdump','linux-x86_64'),('cuda_nvcc','windows-x86_64'),('cuda_crt','windows-x86_64'),
                              ('libnvvm','linux-x86_64'),('libcurand','windows-x86_64'),
                              ('cuda_cudart','windows-x86_64'),
                              ('libcublas','windows-x86_64')]:
        info = doc[component][target]
        path = fetch(base+info['relative_path'], info['sha256'])
        tree = unpack(path,ROOT/'extracted'/path.name)
        if component in ('cuda_cudart','libcublas') and target == 'windows-x86_64':
            libraries = [('cudart','cudart64_13.dll')] if component == 'cuda_cudart' else [('cublas','cublas64_13.dll'),('cublasLt','cublasLt64_13.dll')]
            for library, dllname in libraries:
                dll = next(tree.rglob(dllname))
                exports = subprocess.check_output([os.environ.get('LLVM_READOBJ','llvm-readobj'),'--coff-exports',str(dll)],text=True)
                names = re.findall(r'^\s+Name: (.+)$',exports,re.MULTILINE)
                if not names: raise RuntimeError('Missing CUDA DLL exports: '+dllname)
                lib = dest/'lib/x64'
                lib.mkdir(parents=True,exist_ok=True)
                (lib/('lib'+library+'.def')).write_text('LIBRARY '+dllname+'\nEXPORTS\n'+'\n'.join(names)+'\n')
        for subdir in (('nvvm/libdevice',) if component == 'libnvvm' else ('include','lib')):
            if (tree/subdir).exists(): shutil.copytree(tree/subdir,dest/subdir,dirs_exist_ok=True)
        if target == 'linux-x86_64' and (tree/'bin').exists():
            shutil.copytree(tree/'bin',dest/'bin',dirs_exist_ok=True)
        lic = dest/'licenses'/f'{component}-{target}'
        lic.mkdir(parents=True, exist_ok=True)
        for p in tree.glob('*LICENSE*'): shutil.copy2(p,lic/p.name)
    # Compile/link inputs only. NVIDIA DLLs are installed separately by users.
    for p in dest.rglob('*.dll'): p.unlink()
    for p in dest.rglob('*'): 
        if p.is_file() and (p.name.endswith('_static.lib') or p.name.endswith('_static.a')): p.unlink()
    lib = dest/'lib/x64'
    for p in lib.glob('*.lib'):
        link = lib/('lib'+p.stem+'.a')
        if not link.exists(): link.symlink_to(p.name)
    if not (dest/'lib64').exists(): (dest/'lib64').symlink_to('lib/x64')
    (dest/'version.json').write_text(json.dumps({'cuda': {'name':'CUDA SDK','version':'13.2.0'}}))
    cccl = fetch('https://github.com/NVIDIA/cccl/archive/refs/tags/v2.8.2.tar.gz',
                 '141801ddddf5b911f64f97be66a7c75f98374bebca60f275bedec75b8eeab345')
    tree = unpack(cccl,ROOT/'extracted/cccl-2.8.2-source')
    headers = dest/'include/cccl'
    if headers.exists(): shutil.rmtree(headers)
    for name, subdir in [('cuda','libcudacxx/include/cuda'),('nv','libcudacxx/include/nv'),('cub','cub/cub'),('thrust','thrust/thrust')]:
        shutil.copytree(tree/subdir,headers/name)
    for relative in ('LICENSE','cub/LICENSE.TXT','thrust/LICENSE','libcudacxx/LICENSE.TXT'):
        p=tree/relative
        out=dest/'licenses/cccl-2.8.2'/relative
        out.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,out)
    for relative, symbol in [('cuda/std/detail/libcxx/include/cstdlib','aligned_alloc'),('cuda/std/detail/libcxx/include/ctime','timespec_get')]:
        p=headers/relative
        p.write_text(p.read_text().replace('using ::'+symbol+';', '#if !defined(__MINGW32__)\nusing ::'+symbol+';\n#endif'))
    archive('cuda-cross-13.2.0.tar.xz',dest,'cuda-cross')

def runtimes():
    vulkan = fetch('https://sdk.lunarg.com/sdk/download/1.4.309.0/windows/VulkanRT-1.4.309.0-Components.zip?Human=true',
                   '7d969f4d7b44e387667d3148f61559497c22d50cbe3d50adc9e5409afbce2df1')
    shutil.copy2(vulkan,SOURCES/vulkan.name)
    headers = fetch('https://github.com/KhronosGroup/Vulkan-Headers/archive/refs/tags/v1.4.309.tar.gz',
                    '437925ada160d86ed763d29dcb9318c1bb0d024d7deaf77bc7c170b8eb6b6f10')
    shutil.copy2(headers,SOURCES/'vulkan-headers-1.4.309.tar.gz')
    ort = fetch('https://github.com/microsoft/onnxruntime/releases/download/v1.24.4/onnxruntime-win-x64-1.24.4.zip',
                'd2319fddfb6ea4db99ccc4b60c85c517bcd855721f5daa6a06d40d7cb2ee2357')
    tree = unpack(ort, ROOT/'ort-runtime')
    for pdb in tree.rglob('*.pdb'): pdb.unlink()
    archive('onnxruntime-win-x64-1.24.4-runtime.tar.xz',ROOT/'ort-runtime','ort-runtime')

def npm_sources():
    lock=json.loads((ROOT/'VRCS/package-lock.json').read_text())
    entries = [x for x in lock['packages'].values()
                      if x.get('resolved','').startswith('https:')
                      and ('os' not in x or 'linux' in x['os'])
                      and ('cpu' not in x or 'x64' in x['cpu'])]
    cache = ROOT/'npm-cache-linux-x64'
    if cache.exists(): shutil.rmtree(cache)
    def add(entry):
        algorithm, encoded = entry['integrity'].split('-',1)
        digest = base64.b64decode(encoded).hex()
        content = cache/'_cacache/content-v2'/algorithm/digest[:2]/digest[2:4]/digest[4:]
        if not content.exists():
            path = fetch(entry['resolved'])
            actual = hashlib.new(algorithm,path.read_bytes()).digest()
            if actual != base64.b64decode(encoded): raise RuntimeError('npm integrity mismatch')
            content.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,content)
    with futures.ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(add,entries))
    archive('npm-cache.tar.xz',cache/'_cacache','npm-cache/_cacache')


def installer_tools():
    tools = ROOT/'tauri-tools'
    if tools.exists(): shutil.rmtree(tools)
    tools.mkdir()
    nsis = fetch('https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip',
                 'c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1')
    with zipfile.ZipFile(nsis) as z: z.extractall(tools)
    (tools/'nsis-3.11').rename(tools/'NSIS')
    plugin = fetch('https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll',
                   '5ba143b5db4a87d32d6e7802e033330aae56cbceabe0d1e3ba41948385ad4709')
    dest = tools/'NSIS/Plugins/x86-unicode/additional'
    dest.mkdir(parents=True,exist_ok=True)
    shutil.copy2(plugin,dest)
    webview = fetch('https://go.microsoft.com/fwlink/p/?LinkId=2124703',
                    '87ad3fe4e3faa1b48bad79de1b9269e5eb30574545bb6f5f7c37d51035748caf',
                    filename='MicrosoftEdgeWebview2Setup.exe')
    shutil.copy2(webview,tools)
    for name, checksum in [('cli','16710beec04ee6be052d40276b7b2b7f4de0d8bee2138df94dbf6d6d8014028f'),
                           ('cli-linux-x64-gnu','66ab7f907f12a407bc25214361ffe842f1ad3c63034d4a5f0d0906bc23cf8f06')]:
        pkg = fetch(f'https://registry.npmjs.org/@tauri-apps/{name}/-/{name}-2.11.4.tgz',checksum)
        tree = unpack(pkg,ROOT/(name+'-package'))
        shutil.copytree(tree,tools/'cli/node_modules/@tauri-apps'/name)
    archive('tauri-tools-2.11.4.tar.xz',tools,'tauri-tools')
    source = fetch('https://codeload.github.com/kichik/nsis/tar.gz/refs/tags/v311',
                   '6ba0e34576f95282e4c7ab4347c016eaaa929fb5f20c90c6a28fb84ca1e2724e',
                   filename='nsis-v311.tar.gz')
    shutil.copy2(source,SOURCES/source.name)
