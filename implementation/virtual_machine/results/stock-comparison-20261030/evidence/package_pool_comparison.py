#!/usr/bin/env python3
"""Package immutable comparison evidence; no work is done without --execute.

Run only after all timed measurements have finished. Compression, source hashing,
and archive audit are pinned to host CPUs 8-15. --verify-only also supports offline
validation/extraction of a published package without the original source trees.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile

MIB = 1024 * 1024
CHUNK = MIB
ARMS = {"stock-dram", "patched-dram-disabled", "patched-cxl-hints-unpooled", "patched-cxl-pooled"}
DEFAULT_REPO = Path("/tmp/agent-sandbox-cxl-microvm-main-20261023/implementation/virtual_machine")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


def pin():
    cpus = set(range(8, 16))
    # sched_getaffinity can reflect a narrower taskset inherited from a caller;
    # sched_setaffinity itself checks the actual cgroup/host allowed CPU set.
    os.sched_setaffinity(0, cpus)
    require(set(os.sched_getaffinity(0)) == cpus, "CPUs 8-15 are not all available")
    return sorted(cpus)


def digest_stream(stream):
    h, size = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(CHUNK), b""):
        h.update(chunk)
        size += len(chunk)
    return size, h.hexdigest()


def digest(path):
    with Path(path).open("rb") as stream:
        return digest_stream(stream)[1]


def safe_name(name):
    p = PurePosixPath(name)
    require(bool(name) and not p.is_absolute() and ".." not in p.parts and
            str(p) == name and "\\" not in name, "unsafe archive path: " + name)
    return p


def fingerprint(path):
    s = path.lstat()
    require(stat.S_ISREG(s.st_mode), "not a regular source file: " + str(path))
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def inventory(root, prefix):
    require(root.is_dir() and not root.is_symlink(), "invalid source root: " + str(root))
    included, excluded = {}, []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            require(not (Path(directory) / name).is_symlink(), "symlink directory in source")
        for name in files:
            path = Path(directory) / name
            rel = prefix + "/" + path.relative_to(root).as_posix()
            safe_name(rel)
            before = fingerprint(path)
            if path.name.endswith("-storage.raw"):
                excluded.append(dict(path=rel, bytes=before[2], reason="mutable outer storage disk; intentionally excluded"))
                continue
            included[rel] = dict(source=str(path), signature=before)
    return included, excluded


def read_suite(root, expected, require_snapshot):
    path = root / "report.json"
    d = json.loads(path.read_text())
    require(d.get("schema") == "pool-compare-suite-v1" and d.get("result") == "passed",
            "suite is not complete/passed: " + str(path))
    arms = d.get("arms", [])
    require(len(arms) == len(expected) and {r["arm"] for r in arms} == expected,
            "suite arm set differs: " + str(path))
    children = arms + ([d["stock_snapshot_proof"]] if "stock_snapshot_proof" in d else [])
    require(("stock_snapshot_proof" in d) == require_snapshot, "stock snapshot proof presence differs")
    if require_snapshot:
        require(d["stock_snapshot_proof"]["arm"] == "stock-snapshot", "wrong snapshot arm")
    reports = [dict(path="report.json", sha256=digest(path), result=d["result"])]
    for child in children:
        name = child["report_file"]
        safe_name(name)
        require(name == child["arm"] + "/report.json", "unexpected child report path")
        child_path = root / name
        require(digest(child_path) == child["sha256"], "child report hash mismatch: " + name)
        c = json.loads(child_path.read_text())
        require(c.get("result") == "passed" and c.get("arm") == child["arm"], "child report incomplete")
        reports.append(dict(path=name, sha256=child["sha256"], result=c["result"]))
    for r in reports:
        p = root / r["path"]
        for name, expected_hash in json.loads(p.read_text()).get("source_hashes", {}).items():
            require(Path(name).name == name, "unsafe measured-source name")
            require(digest(p.parent / "measured-sources" / name) == expected_hash,
                    "measured source mismatch: " + str(p.parent / name))
    return d, reports


def assign_groups(files, primary):
    arm_names = {r["arm"] for r in primary["arms"]} | {"stock-snapshot"}
    groups = {}
    for name in sorted(files):
        parts = safe_name(name).parts
        if parts[0] == "tail":
            key = "tail"
        elif parts[1] in arm_names:
            key = "primary-" + parts[1]
        else:
            require(parts[1] in {"report.json", "measured-sources"}, "unexpected primary entry: " + name)
            key = "primary-metadata"
        groups.setdefault(key, []).append(name)
    return groups


class HashReader:
    """Hash the exact source bytes sent to tar, without an extra pre-hash pass."""
    def __init__(self, stream):
        self.stream, self.hash, self.size = stream, hashlib.sha256(), 0

    def read(self, count=-1):
        b = self.stream.read(count)
        self.hash.update(b)
        self.size += len(b)
        return b


def create_archive(path, names, files, manifest, preset):
    with tarfile.open(path, mode="w:xz", format=tarfile.PAX_FORMAT, preset=preset) as archive:
        for name in names:
            source = Path(files[name]["source"])
            require(fingerprint(source) == files[name]["signature"], "source changed before archiving: " + name)
            info = tarfile.TarInfo(name)
            info.size = files[name]["signature"][2]
            info.mode, info.uid, info.gid, info.mtime = 0o644, 0, 0, 0
            with source.open("rb") as stream:
                wrapped = HashReader(stream)
                archive.addfile(info, wrapped)
                require(wrapped.size == info.size and stream.read(1) == b"", "source size changed: " + name)
            require(fingerprint(source) == files[name]["signature"], "source changed while archiving: " + name)
            manifest[name] = dict(bytes=info.size, sha256=wrapped.hash.hexdigest(), archive=path.name)


class ChainedReader(io.RawIOBase):
    """Read validated split archives sequentially, without a large temporary file."""
    def __init__(self, paths):
        self.paths, self.stream = iter(paths), None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.stream is None:
                try:
                    self.stream = next(self.paths).open("rb")
                except StopIteration:
                    return 0
            n = self.stream.readinto(buffer)
            if n:
                return n
            self.stream.close()
            self.stream = None

    def close(self):
        if self.stream is not None:
            self.stream.close()
        super().close()


def archive_stream(directory, record):
    return io.BufferedReader(ChainedReader([directory / p["file"] for p in record["parts"]]), CHUNK)


def seal_archive(path, threshold, part_bytes):
    size, whole_hash = path.stat().st_size, digest(path)
    parts = []
    if size >= threshold:
        with path.open("rb") as source:
            index = 1
            while True:
                data = source.read(part_bytes)
                if not data:
                    break
                part = path.with_name(path.name + ".part%03d" % index)
                part.write_bytes(data)
                parts.append(dict(file=part.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
                index += 1
        record = dict(file=path.name, bytes=size, sha256=whole_hash, split=True, parts=parts)
        with archive_stream(path.parent, record) as stream:
            require(digest_stream(stream) == (size, whole_hash), "split reassembly mismatch")
        path.unlink()
        return record
    return dict(file=path.name, bytes=size, sha256=whole_hash, split=False,
                parts=[dict(file=path.name, bytes=size, sha256=whole_hash)])


def audit_archives(directory, archives, manifest, extraction=None):
    expected = Counter(item["archive"] for item in manifest.values())
    seen = set()
    for record in archives:
        for part in record["parts"]:
            require(Path(part["file"]).name == part["file"], "unsafe archive part name")
            p = directory / part["file"]
            require(p.is_file() and not p.is_symlink() and p.stat().st_size == part["bytes"] and
                    digest(p) == part["sha256"], "archive part bytes/hash mismatch: " + str(p))
        with archive_stream(directory, record) as stream:
            require(digest_stream(stream) == (record["bytes"], record["sha256"]), "archive whole hash mismatch")
        count, total = 0, 0
        with archive_stream(directory, record) as stream, tarfile.open(fileobj=stream, mode="r|xz") as archive:
            for member in archive:
                name = member.name
                safe_name(name)
                require(member.isfile() and name not in seen and name in manifest,
                        "unexpected/duplicate/non-file archive member: " + name)
                item = manifest[name]
                require(item["archive"] == record["file"] and item["bytes"] == member.size,
                        "archive assignment or size differs: " + name)
                h, actual = hashlib.sha256(), 0
                target = None
                if extraction is not None:
                    destination = extraction / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    target = destination.open("xb")
                try:
                    with archive.extractfile(member) as source:
                        for chunk in iter(lambda: source.read(CHUNK), b""):
                            h.update(chunk)
                            actual += len(chunk)
                            if target is not None:
                                target.write(chunk)
                finally:
                    if target is not None:
                        target.close()
                require(actual == item["bytes"] and h.hexdigest() == item["sha256"], "member bytes differ: " + name)
                seen.add(name)
                count += 1
                total += actual
        require(count == expected[record["file"]], "archive member count differs")
        require(record["member_count"] == count and record["uncompressed_bytes"] == total,
                "archive metadata counts differ")
        emit("archive_audit_passed", archive=record["file"], members=count, bytes=total)
    require(seen == set(manifest), "archive membership does not exactly cover source manifest")


def replay_text():
    return """# Offline evidence replay

Run these commands from this package directory. They check the recorded evidence;
they do not rerun the VMs or require the intentionally excluded `*-storage.raw`
outer disks. Archive paths preserve the reports' original relative references.

```sh
sha256sum -c SHA256SUMS
python3 package_pool_comparison.py --verify-only . --extract-to /tmp/pool-comparison-replay
```

The second command validates every part, the SHA-256 of each complete compressed
archive, exact tar-member membership, and the size/SHA-256 of every extracted file.
It streams split parts in their recorded order; no manual concatenation is needed.
The extraction destination must not already exist. Validation runs on CPUs 8-15;
on a replay host with a different CPU topology, use `--no-affinity`.

When this package is in the checkout under
`implementation/virtual_machine/results/stock-comparison-20261030/evidence`,
the following command locates the verifier directory through its ancestors.
If the evidence was copied elsewhere, set `VM_IMPL` explicitly to the checkout's
`implementation/virtual_machine` directory instead.

```sh
VM_IMPL=$(python3 -c 'from pathlib import Path; print(next(p for p in Path.cwd().parents if (p / "verify_pool_compare.py").is_file()))')
```

Then replay the primary suite and the tail stock arm:

```sh
python3 "$VM_IMPL/verify_pool_compare.py" /tmp/pool-comparison-replay/primary/report.json --negative-tests --out /tmp/pool-comparison-primary-audit.json
python3 "$VM_IMPL/verify_pool_compare.py" /tmp/pool-comparison-replay/tail/stock-dram/report.json --negative-tests --out /tmp/pool-comparison-tail-audit.json
python3 "$VM_IMPL/verify_stock_snapshot.py" /tmp/pool-comparison-replay/primary/stock-snapshot/report.json --out /tmp/pool-comparison-stock-snapshot-audit.json
```

The primary suite contains four timed arms and the separate stock snapshot proof.
The tail contains only an additional stock-dram run; it is not a full four-arm
suite, so the tail command selects its child report. Both trees include the exact
measured harness sources. `manifest.json` lists every archived file's bytes/hash;
`metadata.json` records report hashes, exclusions, and archive/part hashes.

For manual extraction, concatenate a split archive's `.part001`, `.part002`, ...
in the order listed in `metadata.json`, verify the resulting full archive hash,
then use `tar -xJf`. Extract every archive into one directory; this creates sibling
`primary/` and `tail/` directories. The automated command above also audits the
individual member hashes and rejects unsafe or duplicate paths.
"""


def package(args):
    cpus = pin()
    primary, tail, target = args.primary.resolve(), args.tail.resolve(), args.output.resolve()
    require(not target.exists(), "output already exists: " + str(target))
    require(not target.is_relative_to(primary) and not target.is_relative_to(tail), "output is inside source")
    pd, pr = read_suite(primary, ARMS, True)
    _, tr = read_suite(tail, {"stock-dram"}, False)
    files, excluded = inventory(primary, "primary")
    tail_files, tail_excluded = inventory(tail, "tail")
    files.update(tail_files)
    excluded.extend(tail_excluded)
    groups = assign_groups(files, pd)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="." + target.name + ".stage-", dir=target.parent))
    emit("package_started", stage=str(stage), files=len(files), excluded_disks=len(excluded), cpus=cpus)
    try:
        manifest, archives = {}, []
        for key, names in sorted(groups.items()):
            path = stage / (key + ".tar.xz")
            emit("archive_started", archive=path.name, members=len(names),
                 source_bytes=sum(files[n]["signature"][2] for n in names))
            create_archive(path, names, files, manifest, args.preset)
            record = seal_archive(path, args.max_archive_mib*MIB, args.part_mib*MIB)
            record.update(member_count=len(names), uncompressed_bytes=sum(manifest[n]["bytes"] for n in names))
            archives.append(record)
            emit("archive_created", archive=path.name, compressed_bytes=record["bytes"], parts=len(record["parts"]))
        audit_archives(stage, archives, manifest)
        # Re-read the current source bytes after compression: the archive must
        # match the source now, not only what existed during an earlier pass.
        now, _ = inventory(primary, "primary")
        more, _ = inventory(tail, "tail")
        now.update(more)
        require(set(now) == set(files), "source membership changed during packaging")
        for name, item in files.items():
            source = Path(item["source"])
            require(fingerprint(source) == item["signature"] == now[name]["signature"], "source stat changed: " + name)
            require(digest(source) == manifest[name]["sha256"], "source bytes changed: " + name)
            require(fingerprint(source) == item["signature"], "source changed during final audit: " + name)
        _, final_pr = read_suite(primary, ARMS, True)
        _, final_tr = read_suite(tail, {"stock-dram"}, False)
        require(pr == final_pr and tr == final_tr, "report references changed during packaging")
        for prefix, reports in [("primary", pr), ("tail", tr)]:
            for report in reports:
                require(manifest[prefix+"/"+report["path"]]["sha256"] == report["sha256"],
                        "archived report differs from validated source")
        write_json(stage / "manifest.json", manifest)
        metadata = dict(schema="pool-comparison-package-v1", result="passed",
                        created_utc=datetime.now(timezone.utc).isoformat(), source_roots=dict(primary=str(primary), tail=str(tail)),
                        cpu_affinity=cpus, compression=dict(format="xz", preset=args.preset, threads=1),
                        max_archive_bytes=args.max_archive_mib*MIB, split_part_bytes=args.part_mib*MIB,
                        member_count=len(manifest), uncompressed_bytes=sum(i["bytes"] for i in manifest.values()),
                        manifest_sha256=digest(stage / "manifest.json"), archives=archives,
                        reports=[dict(r, path=prefix+"/"+r["path"]) for prefix, reports in [("primary",pr),("tail",tr)] for r in reports],
                        excluded_files=sorted(excluded, key=lambda r:r["path"]),
                        checks=dict(exact_source_membership=True, archive_member_bytes_sha256=True,
                                    final_source_bytes_sha256=True, complete_archive_sha256=True,
                                    report_references_sha256=True, measured_sources_sha256=True))
        write_json(stage / "metadata.json", metadata)
        (stage / "REPLAY.md").write_text(replay_text())
        shutil.copyfile(Path(__file__).resolve(), stage / "package_pool_comparison.py")
        (stage / "SHA256SUMS").write_text("".join(digest(p)+"  "+p.name+"\n" for p in sorted(stage.iterdir()) if p.is_file()))
        os.rename(stage, target)
        emit("package_complete", output=str(target), archives=len(archives), members=len(manifest),
             bytes=sum(p.stat().st_size for p in target.iterdir()))
    except BaseException:
        emit("package_failed", retained_stage=str(stage))
        raise


def verify(args):
    if not args.no_affinity:
        pin()
    directory = args.verify_only.resolve()
    metadata = json.loads((directory / "metadata.json").read_text())
    require(metadata["schema"] == "pool-comparison-package-v1" and metadata["result"] == "passed", "invalid package metadata")
    require(digest(directory / "manifest.json") == metadata["manifest_sha256"], "manifest hash mismatch")
    manifest = json.loads((directory / "manifest.json").read_text())
    require(len(manifest) == metadata["member_count"] and
            sum(i["bytes"] for i in manifest.values()) == metadata["uncompressed_bytes"], "manifest totals differ")
    destination = args.extract_to.resolve() if args.extract_to else None
    if destination is not None:
        require(not destination.exists(), "extraction destination already exists")
        destination.mkdir(parents=True)
    audit_archives(directory, metadata["archives"], manifest, destination)
    for r in metadata["reports"]:
        require(r["path"] in manifest and manifest[r["path"]]["sha256"] == r["sha256"], "report hash differs from manifest")
    emit("package_verified", directory=str(directory), extracted_to=str(destination) if destination else None,
         members=len(manifest))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, default=Path("/tmp/pool-compare-suite-v3-20261030"))
    parser.add_argument("--tail", type=Path, default=Path("/tmp/pool-compare-tail-stock-20261030"))
    parser.add_argument("--output", type=Path, default=DEFAULT_REPO / "results/stock-comparison-20261030")
    parser.add_argument("--execute", action="store_true", help="Start hashing/compression; use only after timed runs finish")
    parser.add_argument("--preset", type=int, choices=range(10), default=6)
    parser.add_argument("--max-archive-mib", type=int, default=100)
    parser.add_argument("--part-mib", type=int, default=40)
    parser.add_argument("--verify-only", type=Path, metavar="PACKAGE_DIRECTORY")
    parser.add_argument("--extract-to", type=Path)
    parser.add_argument("--no-affinity", action="store_true", help="Offline replay only: allow another host CPU topology")
    args = parser.parse_args()
    require(0 < args.part_mib < args.max_archive_mib, "invalid archive/part sizes")
    require(not (args.execute and args.verify_only), "choose package or verification")
    require(not args.extract_to or args.verify_only, "--extract-to requires --verify-only")
    require(not args.no_affinity or args.verify_only, "--no-affinity is for offline replay only")
    if args.verify_only:
        verify(args)
    elif args.execute:
        package(args)
    else:
        emit("ready_no_io", primary=str(args.primary), tail=str(args.tail), output=str(args.output),
             action="After timed measurements finish, rerun with --execute; CPUs 8-15 will be used")


if __name__ == "__main__":
    main()
