"""Check independent mapper evidence rejects aliasing and stale identity."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from verify_swe_handoff import Evidence, handle, placement, PAGE, MIB


class MapperEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {'version': 4, 'run_epoch': '2', 'allocation_generation': 7,
            'phys_base': 0x100000000, 'device_bytes': 16*1024*MIB, 'mappings': [],
            'handle': {'abi': 4, 'count': 2, 'state': 1, 'owner_epoch': 2, 'allocation_generation': 7,
                       'phys_base': 0x100000000, 'device_bytes': 16*1024*MIB,
                       'exhausted': 0, 'reserved': [0, 0, 0, 0]}}
        self.plan = {'mappings': [{'gpa': 0, 'offset': 2*MIB}, {'gpa': PAGE, 'offset': 2*MIB+PAGE}],
                     'allocation_generation': 7}

    def evidence(self):
        data = json.dumps(self.manifest).encode()
        (self.root/'mapper.json').write_bytes(data)
        return Evidence({'receipts': [{'file': 'mapper.json', 'sha256': hashlib.sha256(data).hexdigest()}]}, self.root)

    def test_private_coverage(self):
        _, private = placement(self.evidence(), 'mapper.json', 2*PAGE, self.plan)
        self.assertEqual(private, {2*MIB, 2*MIB+PAGE})

    def test_rehashed_alias_is_rejected(self):
        self.plan['mappings'][1]['offset'] = self.plan['mappings'][0]['offset']
        with self.assertRaisesRegex(ValueError, 'alias'):
            placement(self.evidence(), 'mapper.json', 2*PAGE, self.plan)

    def test_rehashed_foreign_frame_is_rejected(self):
        self.plan['mappings'][1]['offset'] = 4096*MIB
        with self.assertRaisesRegex(ValueError, 'geometry'):
            placement(self.evidence(), 'mapper.json', 2*PAGE, self.plan)

    def test_stale_epoch_or_abi_is_rejected(self):
        for field, value in (('owner_epoch', 1), ('allocation_generation', 6), ('abi', 2), ('reserved', [1, 0, 0])):
            bad = copy.deepcopy(self.manifest)
            bad['handle'][field] = value
            with self.assertRaises(ValueError):
                handle(bad, 2*PAGE)

    def test_receipt_cannot_hide_changed_bytes(self):
        evidence = self.evidence()
        (self.root/'mapper.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            evidence.read('mapper.json')


class WorkloadEvidence(unittest.TestCase):
    """A small complete raw transcript exercises the independent archive path."""
    def setUp(self):
        import base64
        import io
        import tarfile
        from verify_swe_handoff import sha
        from verify_swe_handoff import SUBMIT
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipts = []
        encode = lambda row: json.dumps(row, separators=(',', ':'))+'\n'
        def evidence(name, data, kind='fixture'):
            if isinstance(data, str): data = data.encode()
            (self.root/name).write_bytes(data)
            self.receipts.append({'file': name, 'sha256': sha(data), 'kind': kind})
            return name
        environment = {'cwd': '/testbed', 'interpreter': ['bash', '-c']}
        command = 'submit'
        output = SUBMIT+b'\ndiff\n'
        source = {'info': {'config': {'environment_type': 'fixture.DockerEnvironment', 'environment': environment},
                           'exit_status': 'Submitted', 'submission': 'diff\n'},
                  'messages': [{'role': 'assistant', 'extra': {'actions': [{'command': command}]}}]}
        (self.root/'source.traj.json').write_text(json.dumps(source))
        source_hash = sha((self.root/'source.traj.json').read_bytes())
        ready = {'event': 'trace_ready', 'boot_id': 'boot', 'pid': 1, 'command_count': 1}
        start = {'event': 'trace_start', 'label': 'run', 'index': 0, 'command_sha256': sha(command.encode()), 'start_s': 1}
        end = {'event': 'trace_end', 'label': 'run', 'index': 0, 'next_index': 1, 'command_sha256': start['command_sha256'],
               'start_s': 1, 'end_s': 2, 'rc': 0, 'timed_out': False, 'output_sha256': sha(output)}
        identities = []
        for index in (0, 1):
            identities.append({'event': 'trace_identity', 'label': f'id{index}', 'boot_id': 'boot', 'pid': 1,
                'next_index': index, 'sentinel_sha256': sha((f'boot 1 {sha(output)}\n' if index else 'boot 0\n').encode()),
                'fd_inode': 7, 'fd_deleted': True, 'fd_ordinal': index, 'fd_line': f'FD-boot-{index}',
                'fd_position': sum(len(f'FD-boot-{i}\n') for i in range(index+1))})
        payload = {'results/000.command': command.encode(), 'results/000.out': output,
            'results/000.json': encode(end).encode(), 'results/000.block-before': b'0 '*11,
            'results/000.block-after': b'0 '*11, 'results/complete.json': b'{"completed_commands":1}',
            'results/dmesg.txt': b'', 'results/state': f'boot 1 {sha(output)}\n'.encode(),
            'results/guest-mounts.txt': b'/dev/vda /lower ext4 ro 0 0\ntmpfs /ram tmpfs rw 0 0\n'
                                         b'overlay /newroot overlay rw,upperdir=/ram/upper 0 0\n'}
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            for name, data in payload.items():
                item = tarfile.TarInfo(name); item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
        data = archive.getvalue()
        exported = {'event': 'trace_export', 'label': 'export', 'bytes': len(data), 'sha256': sha(data)}
        for name, row in (('ready', ready), ('id0', identities[0]), ('end', end), ('id1', identities[1]), ('export', exported)):
            evidence(name, encode(row))
        evidence('launch', 'EXEC_PROCESS 12 34\n')
        evidence('archive', data)
        fence = evidence('fence', 'FENCED 12 34\n')
        serial = ''.join(encode(row) for row in (ready, identities[0], start, end, identities[1], exported))
        serial += f'TRACE_EXPORT_BEGIN export {len(data)} {sha(data)}\n'+base64.b64encode(data).decode()+'\nTRACE_EXPORT_END export\n'
        evidence('serial', serial, 'final-guest-log')
        apis = []
        for index, (path, body) in enumerate((('/machine-config', {'vcpu_count': 1, 'mem_size_mib': 2048}),
                    ('/drives/data', {'is_read_only': True, 'path_on_host': '/disk/sandbox.raw'}))):
            response = {'status': 204}
            name = evidence(f'api{index}', 'LAB_API_JSON '+encode(response))
            apis.append({'path': path, 'request': body, 'response': response, 'file': name})
        measured = self.root/'measured-sources'; measured.mkdir()
        (measured/'fixture.py').write_bytes(b'fixture')
        artifacts = {name: 'hash' for name in ('firecracker', 'l1-kernel', 'l2-vmlinux', 'pool_memory_module.ko', 'sample-initramfs.cpio')}
        self.report = {'schema': 'swe-handoff-trace-v1', 'result': 'passed', 'mode': 'baseline',
            'arguments': {'memory_mib': 2048}, 'source_hashes': {'fixture.py': sha(b'fixture')},
            'source_trace': {'file': 'source.traj.json', 'sha256': source_hash},
            'trace': {'name': 'sample', 'trace_sha256': source_hash, 'environment': environment, 'image_sha256': 'hash',
                      'image_bytes': 4096, 'command_count': 1, 'commands': [{'command': command}],
                      'handoff_before': 0, 'effective_environment': {}},
            'commands': [{'receipt': end, 'file': 'end', 'vm': 1, 'role': 'A', 'tier': 'dram', 'guest_duration_s': 1}],
            'control_commands': [{'receipt': row, 'file': name} for name, row in
                                 (('id0', identities[0]), ('end', end), ('id1', identities[1]), ('export', exported))],
            'boot': {'receipt': ready, 'file': 'ready'}, 'launches': [{'directory': '/run/example', 'epoch': 0, 'role': 'A', 'file': 'launch'}],
            'cleanup': [{'directory': '/run/example', 'role': 'A', 'file': fence, 'fenced': True}],
            'guest_export': {'receipt': exported, 'file': 'export'}, 'guest_archive': 'archive', 'guest_archive_sha256': sha(data),
            'guest_duration_s': 1, 'receipts': self.receipts, 'api_events': apis, 'allocator_events': [], 'transitions': [], 'audits': [],
            'fixture': {'inputs': {**{key: {'sha256': 'hash'} for key in ('firecracker', 'l1_kernel', 'l2_kernel')}, 'kvm_modules': {}},
                        'artifacts': artifacts},
            'config': {**dict.fromkeys(('host_kernel', 'qemu_sha256', 'l1_vcpus', 'l1_memory_mib', 'shared_mib', 'cxl_mib', 'dax_guest', 'dax_4k'), 'fixture'),
                       'home_storage_image': {'template_sha256': 'hash'}}}

    def verify(self):
        from verify_swe_handoff import verify
        path = self.root/'report.json'
        path.write_text(json.dumps(self.report))
        return verify(path)

    def test_complete_raw_command_and_diff(self):
        self.assertEqual(self.verify()['commands'], 1)

    def test_changed_submission_is_rejected(self):
        source = json.loads((self.root/'source.traj.json').read_text())
        source['info']['submission'] = 'different diff\n'
        (self.root/'source.traj.json').write_text(json.dumps(source))
        digest = hashlib.sha256((self.root/'source.traj.json').read_bytes()).hexdigest()
        self.report['source_trace']['sha256'] = self.report['trace']['trace_sha256'] = digest
        with self.assertRaisesRegex(ValueError, 'final submission differs'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
