#!/usr/bin/env python3
"""Snapshot cut must distinguish saved ACKs from abandoned source-only ACKs."""
import json
import unittest

from verify_pool_discard import Audit
from verify_pool_discard_protocol import correlate


def record(batch, gpa, epoch="0"):
    row = dict(phase="mapped",vm_id="1",run_epoch=epoch,batch=batch,queue_head=0,
               requested=[dict(gpa=gpa,bytes=4096)],discarded=[],requested_pages=1,
               discarded_pages=0,skipped_busy_pages=0,hints_removed=0,copied_bytes=0,host_work_ns=100)
    return ["POOL_DISCARD_JSON "+json.dumps(row), "POOL_DISCARD_JSON "+json.dumps(dict(row,phase="ack"))]


def fixture():
    cutoff = dict(version=1,phase="complete",vm_id="1",run_epoch="0",discard_sequence=1,
                  snapshot_path="/run/lab/vm1-epoch0/state-snapshot",mem_file_path="/run/lab/vm1-epoch0/ram-snapshot")
    lines = record(1,4096)+["POOL_DISCARD_SNAPSHOT_JSON "+json.dumps(cutoff)]+record(2,8192)
    e = object.__new__(Audit)
    e.blobs = {"0000-v1-e0-log.bin":"\n".join(lines).encode(),
               "0001-vm1-final-log.bin":"\n".join(record(1,8192,"1")).encode()}
    e.order = {name:i for i,name in enumerate(e.blobs)}
    e.report = {"api_timings":[dict(path="/snapshot/create",request=dict(snapshot_type="Full",
                    snapshot_path=cutoff["snapshot_path"],mem_file_path=cutoff["mem_file_path"]))]}
    return e


class SnapshotCutTests(unittest.TestCase):
    def test_abandoned_source_ack_is_not_counted(self):
        epochs = fixture().host_epochs(1,1)
        self.assertEqual(epochs[0]["abandoned_after_snapshot_batches"],1)
        trace=[]
        for seq,gpa in ((1,4096),(2,8192)):
            for index,phase in enumerate((0,1,2)):
                trace.append(f'worker [000] .... {seq}.{index:06d}: pool_free_report: seq={seq} phase={phase} index=0 nents=1 gpa={gpa if phase==1 else 0} bytes={4096 if phase==1 else 0} result=0')
        result=correlate("\n".join(trace),epochs)
        self.assertEqual(result["completed_batches"],2)
        self.assertEqual([b["run_epoch"] for b in result["batches"]],["0","1"])

    def test_missing_cutoff_fails_closed(self):
        e=fixture();name=next(iter(e.blobs))
        e.blobs[name]=b"\n".join(line for line in e.blobs[name].splitlines() if b"SNAPSHOT" not in line)
        with self.assertRaisesRegex(ValueError,"snapshot cutoff"): e.host_epochs(1,1)

    def test_cutoff_cannot_invent_saved_ack(self):
        e=fixture();name=next(iter(e.blobs))
        e.blobs[name]=e.blobs[name].replace(b'"discard_sequence": 1',b'"discard_sequence": 2')
        with self.assertRaisesRegex(ValueError,"log ordering"): e.host_epochs(1,1)

    def test_cutoff_cannot_split_mapping_and_ack(self):
        e=fixture();name=next(iter(e.blobs));lines=e.blobs[name].splitlines()
        lines[1],lines[2]=lines[2],lines[1];e.blobs[name]=b"\n".join(lines)
        with self.assertRaisesRegex(ValueError,"log ordering"): e.host_epochs(1,1)

    def test_cutoff_must_bind_actual_snapshot_request(self):
        e=fixture();e.report["api_timings"][0]["request"]["mem_file_path"]="/dev/dax0.0"
        with self.assertRaisesRegex(ValueError,"snapshot API"): e.host_epochs(1,1)

    def test_duplicate_cutoff_is_ambiguous(self):
        e=fixture();name=next(iter(e.blobs));lines=e.blobs[name].splitlines()
        e.blobs[name]=b"\n".join(lines+[lines[2]])
        with self.assertRaisesRegex(ValueError,"snapshot cutoff"): e.host_epochs(1,1)


if __name__ == "__main__": unittest.main()
