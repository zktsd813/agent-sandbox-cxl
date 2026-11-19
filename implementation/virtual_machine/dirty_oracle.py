"""Stopped-boundary oracle for the file-backed lab; the hash executable is read-only.

Only the controller ACK cache line is written by L0. It is a lab gate, never
part of the handoff protocol. Failure is acknowledged as failure, never resumed.
"""
import array
import hashlib
import json
import mmap
import os
from pathlib import Path
import struct
import subprocess
import threading
import time

PAGE = 4096
MIB = 1024 * 1024


def kernel_receipt(memory, gate, pages):
    """Read both saved lab layouts; v2 separately reports initial control INV."""
    values=struct.unpack_from('<8Q',memory,gate+MIB-64)
    if values[5] not in (1,2):
        values=(*struct.unpack_from('<6Q',memory,gate+80),0,0)
    if values[5] not in (1,2):
        return None
    if values[5]==2 and (values[6] not in (0,64+(pages*16+63)//64) or values[7]):
        raise ValueError('Kernel observer initialization receipt differs from emitted metadata ranges')
    return values


class DirtyOracle:
    def __init__(self, config, output, metadata, arena, binary):
        self.output = Path(output) / 'dirty-oracle'
        self.output.mkdir()
        self.metadata, self.arena = metadata, arena
        self.ept_adopt = config.get("ept_adopt", False)
        self.ept_hash = None
        self.gate = metadata + 40 * MIB
        self.backing = Path(config['cxl_backing'])
        self.file = self.backing.open('r+b')
        self.mem = mmap.mmap(self.file.fileno(), 0)
        self.binary = str(Path(binary).resolve(strict=True))
        self.rows, self.error = [], None
        self.records, self.consumed = {}, {}
        self.mapping = None
        self.line_mode = os.environ.get("CXL_ORACLE_LINES") == "1"
        self.line_state = self.output/"line-state"
        self.handoff_state = self.output/"handoff-line-state"
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='dirty-oracle', daemon=True)
        self.thread.start()

    def run(self):
        previous = None
        sequence = 0
        accumulated = 0
        try:
            while not self.stop_event.wait(.001):
                request = struct.unpack_from('<Q', self.mem, self.gate)[0]
                if request == sequence:
                    continue
                if request != sequence + 1:
                    raise ValueError('Skipped oracle boundary')
                _, phase, pages, hva, used, collect_ns = struct.unpack_from('<6Q', self.mem, self.gate)
                if not 0 < pages <= 1 << 20 or phase not in (1, 2, 3, 4, 5, 6):
                    raise ValueError('Invalid oracle geometry/phase')
                maintained, node = struct.unpack_from('<2Q', self.mem, self.gate+48)
                extra = struct.unpack_from('<16Q', self.mem, self.gate+128) if maintained else None
                words = (pages+63)//64
                offset = 256 if maintained else 128
                count = 6 if maintained else 3
                bitmaps = self.mem[self.gate+offset:self.gate+offset+words*8*count]
                union, kernel, device = [int.from_bytes(bitmaps[i*words*8:(i+1)*words*8], 'little') for i in range(3)]
                coverage = self.check_maintenance(phase, node, pages, used, extra, bitmaps) if maintained else {}
                receipts=kernel_receipt(self.mem,self.gate,pages) if maintained else None
                if receipts:
                    wb,inv,log,read,control,version,initial,_=receipts
                    if phase in (1,2,4,6) and wb!=union.bit_count()*64:
                        raise ValueError('Kernel RAM CLWB count differs from listed pages*64')
                    if phase in (3,5) and inv!=coverage['invalidate_lines']:
                        raise ValueError('Kernel RAM invalidation count differs from emitted ranges')
                    coverage['kernel_emitted_lines']={'ram':wb if phase in (1,2,4,6) else inv,
                        'objects':coverage['kernel_writeback_lines'] if phase==2 else coverage['kernel_invalidate_lines'] if phase==3 else 0,
                        'log':log if phase in (1,2,4,6) else read,'control':control if phase in (1,2,3) else 0,
                        'control_init_invalidate':initial if version==2 else None}
                    coverage['instruction_counter_version']=version
                elif self.line_mode:
                    raise ValueError('Line oracle requires kernel cache instruction receipts')
                ept_proof = {}
                if self.ept_adopt and phase in (2, 3):
                    digest = hashlib.sha256(self.mem[self.arena:self.arena+used]).hexdigest()
                    if phase == 2:
                        self.ept_hash = digest
                    elif digest != self.ept_hash:
                        raise ValueError('Adopt changed the released EPT table contents')
                    ept_proof = dict(ept_sha256=digest, ept_hash_unchanged=phase==3)
                if phase in (3, 4, 5):
                    if phase == 4: accumulated |= union
                    row = dict(sequence=request, phase=phase, pages=pages, hash_boundary=False,
                        missing_pages=0, other_changed_pages=0, listed_pages=union.bit_count(),
                        collect_ns=collect_ns, **coverage, **ept_proof)
                    self.rows.append(row)
                    (self.output/f'{request}.bitmap').write_bytes(bitmaps)
                    (self.output/'summary.json').write_text(json.dumps(self.rows,indent=2)+'\n')
                    sequence=request
                    struct.pack_into('<Q',self.mem,self.gate+72,1)
                    struct.pack_into('<Q',self.mem,self.gate+64,request)
                    continue
                started = time.monotonic_ns()
                path = self.output / f'{request}.hashes'
                command=[self.binary,str(self.backing),str(path)]
                if self.line_mode:
                    command += [str(self.line_state),str(self.output/f'{request}.lines')]
                subprocess.run(command, check=True)
                handoff_lines=None
                if self.line_mode and phase in (1,2):
                    handoff_path=self.output/f'{request}.handoff-lines'
                    subprocess.run([self.binary,str(self.backing),str(path)+'.handoff',str(self.handoff_state),str(handoff_path)],check=True)
                    Path(str(path)+'.handoff').unlink()
                    handoff_lines=handoff_path.read_bytes()
                current = array.array('Q')
                with path.open('rb') as stream:
                    current.fromfile(stream, self.backing.stat().st_size // PAGE)
                path.unlink()  # Retain two hash arrays in DRAM, no accumulating raw dumps.
                if union != kernel | device:
                    raise ValueError('Ledger union mismatch')
                table=self.mem[self.metadata+PAGE:self.metadata+PAGE+pages*16]
                if phase==1:
                    self.mapping=table
                    (self.output/'mapping.bin').write_bytes(table)
                elif table!=self.mapping:
                    raise ValueError('Immutable mapper changed between clean boundaries')
                offsets = [struct.unpack_from('<Q', table, i*16)[0] for i in range(pages)]
                if len(set(offsets)) != pages or any(o % PAGE or o >= len(self.mem) for o in offsets):
                    raise ValueError('Invalid persistent mapper table')
                guest = set(o // PAGE for o in offsets)
                changed = set() if previous is None else {i for i,(a,b) in enumerate(zip(previous,current)) if a != b}
                changed_gpa = [i for i,o in enumerate(offsets) if o//PAGE in changed]
                accumulated |= union
                checked_bitmap = accumulated.to_bytes(words*8, "little")
                missing = [i for i in changed_gpa if not (checked_bitmap[i//8] >> (i%8)) & 1]
                # KVM bump cannot shrink. First baseline may precede export, so
                # classify the whole reserved arena then use actual used bytes.
                root_bump, root_vm = (0, 0) if self.ept_adopt else struct.unpack_from('<2Q', self.mem, self.arena+8)
                # This lab admits one graph and starts its first private slot
                # at +16 MiB. Reuse keeps that slot. Legacy roots hold the VM.
                private = root_vm == 0 and root_bump >= 32*MIB
                arena_start = self.arena + (16*MIB if private else 0)
                arena_end = arena_start + (used or (16 if private or self.ept_adopt else 128)*MIB)
                outside = changed - guest
                objects = {i for i in outside if arena_start <= i*PAGE < arena_end}
                control = {i for i in outside if self.metadata <= i*PAGE < self.metadata+64*MIB}
                other = outside - objects - control
                row = dict(sequence=request, phase=phase, pages=pages, ram_hva=hva, hash_boundary=True, **coverage, **ept_proof,
                           changed_guest_pages=len(changed_gpa), checked_pages=accumulated.bit_count(), missing_pages=len(missing),
                           missing_gpa_pages=missing[:32], listed_pages=union.bit_count(),
                           unchanged_listed_pages=union.bit_count()-sum((bitmaps[i//8] >> (i%8))&1 for i in changed_gpa),
                           kvm_only=(kernel & ~device).bit_count(), device_only=(device & ~kernel).bit_count(),
                           both=(kernel & device).bit_count(), object_changed_pages=len(objects),
                           control_changed_pages=len(control), other_changed_pages=len(other),
                           other_file_pages=sorted(other)[:32], collect_ns=collect_ns,
                           arena_bytes=used, object_base_offset=arena_start, object_limit_offset=arena_end,
                           oracle_ns=time.monotonic_ns()-started)
                unchanged_origins={'kvm_only':0,'device_only':0,'both':0}
                changed_origins={'kvm_only':0,'device_only':0,'both':0,'unlisted':0}
                changed_set=set(changed_gpa)
                kb=bitmaps[words*8:words*16];db=bitmaps[words*16:words*24]
                for page in range(pages):
                    k=bool((kb[page//8]>>(page%8))&1);d=bool((db[page//8]>>(page%8))&1)
                    if k or d:
                        origin='both' if k and d else 'kvm_only' if k else 'device_only'
                        (changed_origins if page in changed_set else unchanged_origins)[origin]+=1
                    elif page in changed_set: changed_origins['unlisted']+=1
                row['unchanged_origin_pages']=unchanged_origins
                row['changed_origin_pages']=changed_origins
                if self.line_mode:
                    counts=(self.output/f'{request}.lines').read_bytes()
                    def line_totals(values):
                        # Oracle gate writes are excluded from owner control traffic.
                        logs=set(range((self.metadata+32*MIB)//PAGE,(self.metadata+33*MIB)//PAGE))
                        gate=set(range(self.gate//PAGE,(self.gate+MIB)//PAGE))
                        controls=set(range(self.metadata//PAGE,(self.metadata+64*MIB)//PAGE))
                        arena_pages=range(arena_start//PAGE,(arena_end+PAGE-1)//PAGE)
                        return dict(ram=sum(values[o//PAGE] for o in offsets),
                            objects=sum(values[i] for i in arena_pages),log=sum(values[i] for i in logs),
                            control=sum(values[i] for i in controls-logs-gate),oracle=sum(values[i] for i in gate),other=sum(values[i] for i in other))
                    row['changed_lines']=line_totals(counts)
                    row['handoff_changed_lines']=line_totals(handoff_lines) if handoff_lines is not None else None
                    row['line_counts_sha256']=hashlib.sha256(counts).hexdigest()
                    if phase==2 and handoff_lines is not None:
                        row['handoff_changed_pages']=sum(handoff_lines[o//PAGE]>0 for o in offsets)
                    if len(counts)!=len(current): raise ValueError('Invalid line count geometry')
                    row['changed_region_pages']={'ram':len(changed_gpa),'objects':len(objects),'control_including_log_and_oracle':len(control),'other':len(other)}
                    if sum(counts[o//PAGE]>0 for o in offsets)!=len(changed_gpa):
                        raise ValueError('Page and line hash oracles disagree')
                changed_bitmap=bytearray(words*8)
                for page in changed_gpa: changed_bitmap[page//8] |= 1 << (page%8)
                (self.output/f'{request}.checked').write_bytes(checked_bitmap)
                (self.output/f'{request}.changed').write_bytes(changed_bitmap)
                row['changed_bitmap_sha256']=hashlib.sha256(changed_bitmap).hexdigest()
                row['bitmap_sha256']=hashlib.sha256(bitmaps).hexdigest()
                self.rows.append(row)
                (self.output/f'{request}.bitmap').write_bytes(bitmaps)
                (self.output/'summary.json').write_text(json.dumps(self.rows, indent=2)+'\n')
                if missing or other:
                    raise ValueError(f'Missing guest pages {missing[:16]}, unexplained outside pages {sorted(other)[:16]}')
                if previous is None and phase != 1:
                    raise ValueError('Missing stopped baseline')
                previous, sequence = current, request
                accumulated = 0
                struct.pack_into('<Q', self.mem, self.gate+72, 1)
                struct.pack_into('<Q', self.mem, self.gate+64, request)
        except BaseException as exc:
            self.error = repr(exc)
            (self.output/'error.txt').write_text(self.error+'\n')
            struct.pack_into('<Q', self.mem, self.gate+72, 2)
            struct.pack_into('<Q', self.mem, self.gate+64, request)

    def check_maintenance(self, phase, node, pages, arena, extra, bitmaps):
        if node not in (1,2):
            raise ValueError('Missing logical host identity')
        epoch, inv_from, inv_to, wb_ns, publish_ns, read_ns, inv_ns, kwb_ns, kinv_ns, kwb_bytes, kinv_bytes, kwb_lines, kinv_lines, kwb_ok, kinv_ok, control_ns = extra
        words=(pages+63)//64
        maps=[int.from_bytes(bitmaps[i*words*8:(i+1)*words*8],'little') for i in range(6)]
        bitmap, _, _, wb, requested, invalidated = maps
        base=self.metadata+32*MIB
        magic, version, cookie0, cookie1, log_pages, next_epoch, slots, stride=struct.unpack_from('<8Q',self.mem,base)
        if (cookie0,cookie1)!=struct.unpack_from('<2Q',self.mem,self.metadata+48):
            raise ValueError('Log cookie differs from the persistent mapper')
        if (magic,version,log_pages,slots,stride)!=(0x4e43484c4f473031,1,pages,8,(16+words*8+63)&~63) or next_epoch!=epoch+1:
            raise ValueError('Oracle observed a foreign or incomplete log header')
        if phase in (1,2,4,6):
            if phase==1:
                self.records.clear()
                self.consumed.clear()
            if epoch != len(self.records)+1:
                raise ValueError('Log publication skipped an epoch')
            slot=base+64+((epoch-1)%slots)*stride
            actual_epoch, count=struct.unpack_from('<2Q',self.mem,slot)
            record=int.from_bytes(self.mem[slot+16:slot+16+words*8],'little')
            if phase==1 and record!=(1<<pages)-1:
                raise ValueError('Bulk initialization omitted RAM')
            if actual_epoch!=epoch or record!=bitmap or count!=record.bit_count():
                raise ValueError('Published log does not equal the harvested list')
            if bitmap & ~wb:
                raise ValueError('Actual guest writeback ranges omit a listed page')
            if phase==2 and (kwb_ok!=1 or kwb_bytes<arena or kwb_lines!=(kwb_bytes+63)//64):
                raise ValueError('Actual kernel writeback receipt omits the arena')
            self.records[epoch]=record
            self.consumed[node]=epoch
        else:
            known=self.consumed.get(node,0)
            if inv_from>known or inv_to!=epoch:
                raise ValueError('Receiver consumed unobserved records')
            expected=(1<<pages)-1 if epoch-inv_from>slots else 0
            if epoch-inv_from<=slots:
                for e in range(inv_from+1,epoch+1):
                    expected |= self.records[e]
            if requested!=expected or bitmap!=expected or expected & ~invalidated:
                raise ValueError('Actual receiver ranges do not cover the log union')
            if phase==3 and (kinv_ok!=1 or kinv_bytes<arena or kinv_lines!=(kinv_bytes+63)//64+(0 if getattr(self,"ept_adopt",False) else 64)):
                raise ValueError('Actual kernel invalidation receipt omits the arena')
            self.consumed[node]=epoch
        return dict(node=node,epoch=epoch,inv_from=inv_from,inv_to=inv_to,
            writeback_ns=wb_ns,publish_ns=publish_ns,log_read_ns=read_ns,invalidate_ns=inv_ns,
            kernel_writeback_ns=kwb_ns,kernel_invalidate_ns=kinv_ns,control_ns=control_ns,
            kernel_writeback_bytes=kwb_bytes,kernel_invalidate_bytes=kinv_bytes,
            kernel_writeback_lines=kwb_lines,kernel_invalidate_lines=kinv_lines,
            writeback_lines=wb.bit_count()*64,invalidate_lines=invalidated.bit_count()*64,
            coverage_verified=True,full_invalidate_fallback=phase in (3,5) and epoch-inv_from>8)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=120)
        if self.thread.is_alive():
            raise RuntimeError('Oracle failed to stop')
        self.line_state.unlink(missing_ok=True)
        self.handoff_state.unlink(missing_ok=True)
        self.mem.close()
        self.file.close()
        if self.error:
            raise RuntimeError(self.error)
