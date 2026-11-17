"""Stopped-boundary oracle for the file-backed lab; the hash executable is read-only.

Only the controller ACK cache line is written by L0. It is a lab gate, never
part of the handoff protocol. Failure is acknowledged as failure, never resumed.
"""
import array
import json
import mmap
from pathlib import Path
import struct
import subprocess
import threading
import time

PAGE = 4096
MIB = 1024 * 1024


class DirtyOracle:
    def __init__(self, config, output, metadata, arena, binary):
        self.output = Path(output) / 'dirty-oracle'
        self.output.mkdir()
        self.metadata, self.arena = metadata, arena
        self.gate = metadata + 40 * MIB
        self.backing = Path(config['cxl_backing'])
        self.file = self.backing.open('r+b')
        self.mem = mmap.mmap(self.file.fileno(), 0)
        self.binary = str(Path(binary).resolve(strict=True))
        self.rows, self.error = [], None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='dirty-oracle', daemon=True)
        self.thread.start()

    def run(self):
        previous = None
        sequence = 0
        try:
            while not self.stop_event.wait(.001):
                request = struct.unpack_from('<Q', self.mem, self.gate)[0]
                if request == sequence:
                    continue
                if request != sequence + 1:
                    raise ValueError('Skipped oracle boundary')
                _, phase, pages, hva, used, collect_ns = struct.unpack_from('<6Q', self.mem, self.gate)
                if not 0 < pages <= 1 << 20 or phase not in (1, 2):
                    raise ValueError('Invalid oracle geometry/phase')
                started = time.monotonic_ns()
                path = self.output / f'{request}.hashes'
                subprocess.run([self.binary, str(self.backing), str(path)], check=True)
                current = array.array('Q')
                with path.open('rb') as stream:
                    current.fromfile(stream, self.backing.stat().st_size // PAGE)
                path.unlink()  # Retain two hash arrays in DRAM, no accumulating raw dumps.
                words = (pages + 63) // 64
                bitmaps = self.mem[self.gate+128:self.gate+128+words*8*3]
                union, kernel, device = [int.from_bytes(bitmaps[i*words*8:(i+1)*words*8], 'little')
                                         for i in range(3)]
                if union != kernel | device:
                    raise ValueError('Ledger union mismatch')
                offsets = [struct.unpack_from('<Q', self.mem, self.metadata+PAGE+i*16)[0] for i in range(pages)]
                if len(set(offsets)) != pages or any(o % PAGE or o >= len(self.mem) for o in offsets):
                    raise ValueError('Invalid persistent mapper table')
                guest = set(o // PAGE for o in offsets)
                changed = set() if previous is None else {i for i,(a,b) in enumerate(zip(previous,current)) if a != b}
                changed_gpa = [i for i,o in enumerate(offsets) if o//PAGE in changed]
                missing = [i for i in changed_gpa if not (bitmaps[i//8] >> (i%8)) & 1]
                # KVM bump cannot shrink. First baseline may precede export, so
                # classify the whole reserved arena then use actual used bytes.
                arena_end = self.arena + (used or 128*MIB)
                outside = changed - guest
                objects = {i for i in outside if self.arena <= i*PAGE < arena_end}
                control = {i for i in outside if self.metadata <= i*PAGE < self.metadata+64*MIB}
                other = outside - objects - control
                row = dict(sequence=request, phase=phase, pages=pages, ram_hva=hva,
                           changed_guest_pages=len(changed_gpa), missing_pages=len(missing),
                           missing_gpa_pages=missing[:32], listed_pages=union.bit_count(),
                           unchanged_listed_pages=union.bit_count()-sum((bitmaps[i//8] >> (i%8))&1 for i in changed_gpa),
                           kvm_only=(kernel & ~device).bit_count(), device_only=(device & ~kernel).bit_count(),
                           both=(kernel & device).bit_count(), object_changed_pages=len(objects),
                           control_changed_pages=len(control), other_changed_pages=len(other),
                           other_file_pages=sorted(other)[:32], collect_ns=collect_ns,
                           arena_bytes=used, oracle_ns=time.monotonic_ns()-started)
                self.rows.append(row)
                (self.output/f'{request}.bitmap').write_bytes(bitmaps)
                (self.output/'summary.json').write_text(json.dumps(self.rows, indent=2)+'\n')
                if missing or other:
                    raise ValueError(f'Missing guest pages {missing[:16]}, unexplained outside pages {sorted(other)[:16]}')
                if previous is None and phase != 1:
                    raise ValueError('Missing stopped baseline')
                previous, sequence = current, request
                struct.pack_into('<Q', self.mem, self.gate+72, 1)
                struct.pack_into('<Q', self.mem, self.gate+64, request)
        except BaseException as exc:
            self.error = repr(exc)
            (self.output/'error.txt').write_text(self.error+'\n')
            struct.pack_into('<Q', self.mem, self.gate+72, 2)
            struct.pack_into('<Q', self.mem, self.gate+64, request)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=120)
        if self.thread.is_alive():
            raise RuntimeError('Oracle failed to stop')
        self.mem.close()
        self.file.close()
        if self.error:
            raise RuntimeError(self.error)
