"""Validate CUDA images embedded in a Windows PE using Linux cuobjdump."""
import argparse
from pathlib import Path
import struct
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('binary', type=Path)
parser.add_argument('artifacts', type=Path)
args = parser.parse_args()
data = args.binary.read_bytes()
if data[:2] != b'MZ':
    raise SystemExit('CUDA application must be a Windows PE')
pe_offset = struct.unpack_from('<I', data, 0x3c)[0]
if data[pe_offset:pe_offset + 4] != b'PE\0\0':
    raise SystemExit('Invalid PE header')
if struct.unpack_from('<H', data, pe_offset + 4)[0] != 0x8664:
    raise SystemExit('CUDA application must be x64')

# Clang embeds NVIDIA fatbinary containers unchanged. Linux cuobjdump does
# not locate them in PE executables, so pass each complete container directly.
magic = bytes.fromhex('50ed55ba')
offset = 0
count = 0
elf_reports = []
ptx_reports = []
with tempfile.TemporaryDirectory(prefix='vrcs-cuda-check-') as scratch:
    while (offset := data.find(magic, offset)) >= 0:
        if offset + 16 > len(data):
            break
        _, version, header_size, payload_size = struct.unpack_from('<IHHQ', data, offset)
        end = offset + header_size + payload_size
        if version != 1 or header_size != 16 or payload_size == 0 or end > len(data):
            offset += 4
            continue
        image = Path(scratch) / f'image-{count:04d}.fatbin'
        image.write_bytes(data[offset:end])
        reports = []
        for option in ['-lelf', '-lptx']:
            result = subprocess.run(['cuobjdump', option, str(image)],
                                    capture_output=True, text=True)
            if result.returncode:
                raise SystemExit(result.stdout + result.stderr)
            reports.append(result.stdout + result.stderr)
        elf, ptx = reports
        for architecture in ['sm_75', 'sm_80', 'sm_86', 'sm_89', 'sm_120a']:
            if architecture not in elf:
                raise SystemExit(f'{image.name}: missing {architecture} cubin')
        if 'sm_89' not in ptx:
            raise SystemExit(f'{image.name}: missing compute_89 PTX fallback')
        label = f'Fatbinary {count}, PE offset 0x{offset:x}\n'
        elf_reports.append(label + elf)
        ptx_reports.append(label + ptx)
        count += 1
        offset = end
if count == 0:
    raise SystemExit('No NVIDIA fatbinary containers found')
args.artifacts.mkdir(parents=True, exist_ok=True)
(args.artifacts / 'cuda-architectures.txt').write_text('\n'.join(elf_reports))
(args.artifacts / 'cuda-ptx.txt').write_text('\n'.join(ptx_reports))
print(f'Validated {count} embedded CUDA fatbinary containers')
