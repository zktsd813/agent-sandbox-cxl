"""Reference committed experiment sources without copying them into results.

This runs at experiment setup or offline verification, never during handoff.
Old result directories with saved sources remain readable. To verify results
elsewhere, set CXL_EXPERIMENT_SOURCE_REPOSITORY to a clone containing the commit.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _blobs(reference):
    commit, paths = reference['commit'], reference['paths']
    _need(reference['version'] == 1 and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit),
          'Invalid experiment source commit')
    _need(paths and all(Path(name).name == name and name not in ('.', '..') for name in paths),
          'Invalid experiment source names')
    for path in paths.values():
        p = PurePosixPath(path)
        _need(not p.is_absolute() and '..' not in p.parts and '\n' not in path and '\r' not in path,
              'Invalid experiment source path')
    repository = os.environ.get('CXL_EXPERIMENT_SOURCE_REPOSITORY', reference['repository'])
    requests = ''.join(f'{commit}:{path}\n' for path in paths.values()).encode()
    data = subprocess.check_output(['git', '-C', repository, 'cat-file', '--batch'], input=requests)
    result, offset = {}, 0
    for name in paths:
        end = data.find(b'\n', offset)
        fields = data[offset:end].split()
        _need(end >= offset and len(fields) == 3 and fields[1] == b'blob',
              'Experiment source commit/path is unavailable')
        size = int(fields[2]); offset = end + 1
        _need(size >= 0 and offset + size < len(data) and data[offset + size] == 10,
              'Truncated experiment source blob')
        result[name] = data[offset:offset + size]
        offset += size + 1
    _need(offset == len(data), 'Unexpected experiment source data')
    return result


def record_sources(directory, names):
    directory = Path(directory).resolve()
    repository = Path(subprocess.check_output(
        ['git', '-C', str(directory), 'rev-parse', '--show-toplevel'], text=True).strip())
    commit = subprocess.check_output(
        ['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
    names = sorted(set(names))
    reference = {'version': 1, 'repository': str(repository), 'commit': commit,
                 'paths': {name: (directory / name).relative_to(repository).as_posix() for name in names}}
    blobs = _blobs(reference)
    hashes = {}
    for name, data in blobs.items():
        _need((directory / name).read_bytes() == data,
              'Commit measured source changes locally before running: ' + name)
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes, reference


def verify_sources(report, root, *, hashes_key='source_hashes', legacy_directory='measured-sources'):
    hashes = report[hashes_key]
    if 'source_reference' in report:
        blobs = _blobs(report['source_reference'])
        _need(set(blobs) == set(hashes), 'Experiment source list changed')
    else:
        _need(all(Path(name).name == name and name not in ('.', '..') for name in hashes),
              'Invalid saved source names')
        blobs = {name: (Path(root) / legacy_directory / name).read_bytes() for name in hashes}
    _need(all(hashlib.sha256(data).hexdigest() == hashes[name] for name, data in blobs.items()),
          'Experiment source differs from recorded revision')
