#!/usr/bin/env python3
"""Adversarial evidence checks for the independent reporting protocol verifier."""
import copy
import json
import unittest

import verify_pool_discard_protocol as verifier


def guest_trace():
    rows = []
    for seq, ranges in ((1, [(4096, 8192), (16384, 4096)]), (2, [(32768, 4096)])):
        events = [(0, 0, 0, 0)] + [(1, i, start, size) for i, (start, size) in enumerate(ranges)] + [(2, 0, 0, 0)]
        for index, (phase, entry, gpa, size) in enumerate(events):
            rows.append(f'worker-1 [000] .... {seq}.{index:06d}: pool_free_report: seq={seq} phase={phase} index={entry} nents={len(ranges)} gpa={gpa} bytes={size} result=0')
    return '\n'.join(rows)


def host_epochs():
    epochs = []
    for epoch, requested, discarded, busy in (
        (0, [{'gpa':4096, 'bytes':8192}, {'gpa':16384, 'bytes':4096}], [{'gpa':4096, 'bytes':4096}], 0),
        (1, [{'gpa':32768, 'bytes':4096}], [], 1),
    ):
        record = dict(phase='mapped', vm_id='1', run_epoch=str(epoch), batch=1, queue_head=0,
                      requested=requested, discarded=discarded,
                      requested_pages=sum(r['bytes'] for r in requested)//4096,
                      discarded_pages=len(discarded), skipped_busy_pages=busy,
                      hints_removed=0, copied_bytes=0, host_work_ns=9000)
        rows = [record, dict(record, phase='ack')]
        epochs.append(dict(vm_id='1', run_epoch=str(epoch), text='\n'.join('PREFIX POOL_DISCARD_JSON '+json.dumps(r) for r in rows)))
    return epochs


def mutate_host(epochs, key, value, phase=None):
    changed = copy.deepcopy(epochs)
    rows = []
    for line in changed[0]['text'].splitlines():
        row = json.loads(line.split('POOL_DISCARD_JSON ', 1)[1])
        if phase is None or row['phase'] == phase:
            row[key] = value
        rows.append('PREFIX POOL_DISCARD_JSON '+json.dumps(row))
    changed[0]['text'] = '\n'.join(rows)
    return changed


class ProtocolEvidenceTests(unittest.TestCase):
    def test_positive_ordered_epochs_and_noop_ack(self):
        r = verifier.correlate(guest_trace(), host_epochs())
        self.assertEqual(r['completed_batches'], 2)
        self.assertEqual([r['batches'][i]['discarded_pages'] for i in (0, 1)], [1, 0])
        self.assertEqual(r['batches'][1]['skipped_busy_pages'], 1)

    def test_copy_is_not_nocopy(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'copied_bytes', 4096))

    def test_guest_ack_without_host_ack(self):
        epochs = host_epochs()
        epochs[0]['text'] = epochs[0]['text'].splitlines()[0]
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), epochs)

    def test_changed_ack_counter(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'host_work_ns', 9001, 'ack'))

    def test_discard_outside_request(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'discarded', [{'gpa':24576,'bytes':4096}]))

    def test_wrong_vm_incarnation(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'run_epoch', '2'))

    def test_guest_sequence_gap(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace().replace('seq=2 ', 'seq=3 '), host_epochs())

    def test_guest_range_mismatch(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace().replace('gpa=16384', 'gpa=20480'), host_epochs())

    def test_partial_page_discard_rejected(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'discarded', [{'gpa':4096,'bytes':4095}]))

    def test_overlapping_request_rejected(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'requested', [{'gpa':4096,'bytes':8192},{'gpa':8192,'bytes':4096}]))

    def test_false_discard_count_rejected(self):
        with self.assertRaises(ValueError):
            verifier.correlate(guest_trace(), mutate_host(host_epochs(), 'discarded_pages', 2))

    def test_trace_overwrite_rejected(self):
        valid='entries: 3\noverrun: 0\ncommit overrun: 0\ndropped events: 0\nread events: 0\n'
        verifier.trace_stats(valid)
        with self.assertRaises(ValueError):
            verifier.trace_stats(valid.replace('overrun: 0', 'overrun: 1', 1))

    def test_nonconsuming_trace_required(self):
        with self.assertRaises(ValueError):
            verifier.trace_stats('entries: 3\noverrun: 0\ncommit overrun: 0\ndropped events: 0\nread events: 1\n')

    def test_incomplete_final_guest_tail_is_not_success(self):
        text=guest_trace().rsplit('\n',1)[0]
        r=verifier.correlate(text,host_epochs())
        self.assertEqual(r['completed_batches'],1)
        self.assertEqual(r['incomplete_guest_tail']['seq'],2)
        self.assertEqual(r['complete_host_tail_batches'],1)


if __name__ == '__main__':
    unittest.main()
