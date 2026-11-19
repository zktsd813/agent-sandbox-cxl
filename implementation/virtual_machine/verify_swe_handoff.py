#!/usr/bin/env python3
"""Independently audit exact SWE command results and VM-private RAM handoff.

No runner/controller imports. Receipts, serial frames, source traces and final
submission are checked independently, including process/FD continuity.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile

from experiment_sources import verify_sources

WARNING = b"/root/.bashrc: line 1: /opt/miniconda3/etc/profile.d/conda.sh: No such file or directory\n"
SUBMIT = b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
FATAL_KERNEL = re.compile(r"Kernel panic|Out of memory:|oom-kill:|Killed process [0-9]+|"
                          r"Buffer I/O error|EXT4-fs error|I/O error, dev vda|"
                          r"blk_update_request: I/O error|\bBUG:|\bkernel BUG at|\bOops:", re.I)


PAGE = 4096
MIB = 1024**2
PRIVATE_START, PRIVATE_END = 2*MIB, 3072*MIB
TRACE_NAMES = {"gin-gonic__gin-3741", "php-cs-fixer__php-cs-fixer-8331", "micropython__micropython-13039"}
def need(ok, message):
    if not ok:
        raise ValueError(message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def fingerprint(data):
    return {"sha256": sha(data), "without_startup_warning_sha256": sha(data.replace(WARNING, b"")),
            "startup_warning_count": data.count(WARNING)}

def original_trace(path):
    """Parse source bytes directly instead of importing the replay implementation."""
    raw = path.read_bytes()
    source = json.loads(raw)
    config = source["info"]["config"]
    need(config["environment_type"].endswith(".DockerEnvironment"), "unsupported trace environment")
    need(not config["environment"].get("forward_env"), "host environment forwarding")
    need(source["info"]["exit_status"] == "Submitted", "source trace did not submit")
    batches = []
    for message in source["messages"]:
        extra = message.get("extra", {})
        if message["role"] == "assistant" and extra.get("actions"):
            batches.append([extra["actions"], []])
        elif batches and message["role"] in ("tool", "user") and "returncode" in extra:
            batches[-1][1].append(extra)
    need(bool(batches), "trace has no actions")
    commands = []
    for number, (actions, observations) in enumerate(batches):
        need(len(actions) == len(observations) or (number == len(batches) - 1 and not observations),
                f"incomplete recorded observations in batch {number}")
        for index, action in enumerate(actions):
            need(isinstance(action["command"], str), "non-text trace command")
            data = action["command"].encode()
            commands.append({"index": len(commands), "command": data,
                             "command_sha256": sha(data),
                             "expected_returncode": observations[index]["returncode"] if observations else None,
                             "source_output": observations[index]["raw_output"].encode() if observations else None})
    need(sum(c["expected_returncode"] is None for c in commands) == 1 and
            commands[-1]["expected_returncode"] is None, "only final submission may lack recorded return code")
    return {"sha256": sha(raw), "commands": commands, "environment": config["environment"],
            "submission": fingerprint(source["info"]["submission"].encode())}

def archive_files(path):
    # Never extract guest-controlled paths to the host filesystem.
    files = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            need(not name.is_absolute() and ".." not in name.parts, f"unsafe archive member {member.name}")
            if member.isdir():
                continue
            need(member.isfile(), f"unexpected archive member type: {member.name}")
            key = str(name)
            need(key not in files, f"duplicate archive member: {key}")
            files[key] = archive.extractfile(member).read()
    return files

def block_stats(data):
    values = [int(value) for value in data.split()]
    need(len(values) >= 11 and min(values) >= 0, "invalid guest block statistics")
    return dict(zip(("read_ios", "read_sectors", "write_ios", "write_sectors"),
                    (values[0], values[2], values[4], values[6])))

def test_summary(command, output):
    """Keep test outcomes/counts while dropping duration and scheduling noise."""
    command = command.decode().strip()
    output = output.decode(errors="replace")
    if command.startswith("go test "):
        packages = sorted(re.findall(r"^(ok|\?|FAIL)[ \t]+([^ \t\r\n]+)(?:[ \t].*)?$", output, re.M))
        need(bool(packages), "Go test command produced no package results")
        return {"kind": "go", "packages": packages}
    if command.startswith("vendor/bin/phpunit ") and "--version" not in shlex.split(command)[1:]:
        counts = re.findall(r"^Tests: ([0-9]+), Assertions: ([0-9]+)", output, re.M)
        if not counts:
            counts = re.findall(r"^OK \(([0-9]+) tests?, ([0-9]+) assertions?\)", output, re.M)
        need(len(counts) == 1, "PHPUnit command produced no unique test/assertion summary")
        return {"kind": "phpunit", "tests": int(counts[0][0]), "assertions": int(counts[0][1])}
    if command.startswith("python3 tests/run-tests.py "):
        performed = re.findall(r"^([0-9]+) tests performed \(([0-9]+) individual testcases\)$", output, re.M)
        passed = re.findall(r"^([0-9]+) tests passed$", output, re.M)
        need(len(performed) == len(passed) == 1, "MicroPython command produced no unique test summary")
        result = {"kind": "micropython", "tests": int(performed[0][0]),
                  "testcases": int(performed[0][1]), "passed": int(passed[0])}
        for status in ("skipped", "failed"):
            matches = re.findall(rf"^([0-9]+) tests {status}: ([^\r\n]+)$", output, re.M)
            need(len(matches) <= 1, f"duplicate MicroPython {status} summary")
            result[status] = {"count": int(matches[0][0]), "names": sorted(matches[0][1].split())} if matches else {"count": 0, "names": []}
            need(result[status]["count"] == len(result[status]["names"]), f"MicroPython {status} count/names disagree")
        need(result["passed"] + result["failed"]["count"] == result["tests"], "MicroPython test counts disagree")
        return result
    return None

def local(root, name):
    need(isinstance(name, str) and not Path(name).is_absolute() and ".." not in Path(name).parts,
         "unsafe artifact path")
    return root / name

def guest(e, event, kind=None):
    expected = event["receipt"]
    rows = [json.loads(line) for line in e.text(event["file"]).splitlines()
            if line.startswith('{"event":')]
    matches = [r for r in rows if r.get("event") == expected["event"] and r.get("label") == expected.get("label")]
    need(matches == [expected] and (kind is None or expected["event"] == kind), "altered guest event")
    return expected

def identity_pair(e, before, after):
    a, b = guest(e, before, "trace_identity"), guest(e, after, "trace_identity")
    for key in ("boot_id", "pid", "next_index", "sentinel_sha256", "fd_inode", "fd_deleted"):
        need(a[key] == b[key], "handoff changed guest " + key)
    need(a["fd_deleted"] is True and b["fd_ordinal"] == a["fd_ordinal"] + 1
         and b["fd_position"] > a["fd_position"], "unlinked FD continuity missing")
    return a, b

def serial_proof(e, report):
    """Bind extracted receipts to complete logs from each fenced VMM."""
    logs = {}
    for cleanup in report["cleanup"]:
        following = [(e.order[name], name) for name, item in e.items.items()
                     if item["kind"] == "final-guest-log" and e.order[name] > e.order[cleanup["file"]]]
        need(following, "missing final guest serial log")
        _, name = min(following)
        need(name not in logs.values(), "reused final guest log")
        logs[cleanup["directory"]] = name
    def execution_log(launch):
        data=e.read(logs[launch['directory']])
        if report['mode']=='ept-adopt' and launch['directory']==report['prepared_seed']['directory']:
            seed=report['prepared_seed'];prefix=e.read(seed['file'])
            need(len(prefix)==seed['bytes'] and sha(prefix)==seed['sha256'] and data.startswith(prefix),
                 'prepared worker serial prefix changed')
            rows=[json.loads(line) for line in prefix.decode().splitlines() if line.startswith('{"event":')]
            need(rows==[guest(e,seed['ready'],'trace_ready')], 'seed executed unexpected trace commands')
            data=data[len(prefix):]
        return data.decode().replace('\r','')
    text = "\n".join(execution_log(launch)
                     for launch in sorted(report["launches"], key=lambda row: row["epoch"]))
    need(not FATAL_KERNEL.search(text) and "TRACE_FATAL" not in text, "fatal guest error in full serial log")
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{"event":')]
    if report["mode"] == "ept-adopt":
        # Each worker serial file contains disjoint visits. Validate ordering inside
        # every file, then merge using the controller's unique command sequence labels.
        def serial_order(row):
            if row["event"] == "trace_ready": return (-1, 0)
            match = re.fullmatch(r"v1e([0-9]+)c([0-9]+)", row.get("label", ""))
            need(match is not None, "worker serial command label missing")
            return (int(match[2]), 0 if row["event"] == "trace_start" else 1)
        for name in logs.values():
            local = [json.loads(line) for line in e.text(name).splitlines() if line.startswith('{"event":')]
            need([serial_order(r) for r in local] == sorted(serial_order(r) for r in local), "worker serial reordered locally")
        rows.sort(key=serial_order)
    need(not any(row["event"] in ("trace_error", "trace_protocol_error") for row in rows), "guest protocol error")
    ready = [row for row in rows if row["event"] == "trace_ready"]
    need(ready == [guest(e, report["boot"], "trace_ready")], "guest reboot/missing ready event")
    controls = [item["receipt"] for item in report["control_commands"]]
    need([row for row in rows if row["event"] not in ("trace_ready", "trace_start")] == controls,
         "full serial/control transcript differs")
    count = report["trace"]["command_count"]
    need([(row["event"], row["index"]) for row in rows if row["event"] in ("trace_start", "trace_end")] ==
         [(kind, i) for i in range(count) for kind in ("trace_start", "trace_end")], "raw command execution order")
    for end in [row for row in rows if row["event"] == "trace_end"]:
        starts = [row for row in rows if row["event"] == "trace_start" and row["label"] == end.get("run_label", end["label"])]
        need(len(starts) == 1 and all(starts[0][key] == end[key] for key in
             ("index", "command_sha256", "start_s")), "command start/end disagreement")
    export = guest(e, report["guest_export"], "trace_export")
    frames = re.findall(rf"^TRACE_EXPORT_BEGIN {re.escape(export['label'])} ([0-9]+) ([0-9a-f]{{64}})\n(.*?)^TRACE_EXPORT_END {re.escape(export['label'])}$",
                        text, re.M | re.S)
    need(len(frames) == 1, "guest archive serial frame absent/duplicate")
    size, checksum, encoded = frames[0]
    data = base64.b64decode("".join(encoded.split()), validate=True)
    need(data == e.read(report["guest_archive"]) and len(data) == int(size) == export["bytes"]
         and sha(data) == checksum == export["sha256"], "guest archive differs from full serial frame")
    return ready[0]

def verify_trace(e, report):
    plan, root = report["trace"], e.root
    source = report["source_trace"]
    if isinstance(source, dict):
        source = source["file"]
    trace = original_trace(local(root, source))
    need(trace["sha256"] == plan["trace_sha256"] and trace["environment"] == plan["environment"],
         "source trace/environment changed")
    if isinstance(report["source_trace"], dict):
        need(trace["sha256"] == report["source_trace"]["sha256"], "recorded source trace hash differs")
    archive = local(root, report["guest_archive"])
    need(sha(archive.read_bytes()) == report["guest_archive_sha256"], "guest archive hash")
    files = archive_files(archive)
    ready = serial_proof(e, report)
    commands = [x for x in report["commands"] if x.get("receipt", {}).get("event") == "trace_end"]
    need(len(commands) == plan["command_count"] == len(trace["commands"]), "trace command count")
    expected_files = {f"results/{i:03d}.{extension}" for i in range(len(commands))
                      for extension in ("command", "out", "json", "block-before", "block-after")}
    cpu_files = {key for key in files if re.fullmatch(r"results/[0-9]{3}\.cpu-(before|after|[a-zA-Z0-9_-]+)", key)}
    if cpu_files:
        required_cpu = {f"results/{i:03d}.cpu-{side}" for i in range(len(commands)) for side in ("before", "after")}
        required_cpu.update(f"results/{row['receipt']['index']:03d}.cpu-{row['receipt']['label']}"
                            for row in report['control_commands'] if row.get('operation') == 'running'
                            and row['receipt']['index'] < len(commands))
        need(cpu_files == required_cpu, "missing/extra per-CPU counter snapshots")
        for name in cpu_files:
            rows = [line.split() for line in files[name].decode().splitlines()]
            need([r[0] for r in rows] == [f"cpu{i}" for i in range(report['arguments'].get('vcpus', 1))]
                 and all(len(r) >= 9 and all(v.isdigit() for v in r[1:]) for r in rows), "invalid per-CPU counters")
    need({key for key in files if key.startswith("results/") and key.split("/")[-1][:3].isdigit()} ==
         expected_files | cpu_files, "missing/extra command artifacts")
    raw_ends, tests, read_ios, remote_reads, duration = [], [], 0, 0, 0.0
    for index, (command, event) in enumerate(zip(trace["commands"], commands)):
        end = guest(e, event, "trace_end")
        need(end["index"] == index and end["next_index"] == index + 1
             and end["command_sha256"] == command["command_sha256"], "command order/bytes changed")
        prefix = f"results/{index:03d}"
        need(files[prefix + ".command"] == command["command"] and json.loads(files[prefix + ".json"]) == end,
             "archive command/end differs from raw receipt")
        need(sha(files[prefix + ".out"]) == end["output_sha256"], "command stdout hash")
        expected = command["expected_returncode"]
        need(end["rc"] == (0 if expected is None else expected) and end["timed_out"] is False,
             "recorded command failed/diverged/timed out")
        if command["source_output"] is not None:
            previous = test_summary(command["command"], command["source_output"])
            if previous is not None:
                actual = test_summary(command["command"], files[prefix + ".out"])
                need(actual == previous, "test outcomes differ from original trace")
                tests.append({"index": index, **actual})
        need(end["end_s"] >= end["start_s"] and (not raw_ends or end["start_s"] >= raw_ends[-1]["end_s"]),
             "guest command clock/order")
        elapsed = end["end_s"] - end["start_s"]
        need(math.isfinite(elapsed) and math.isclose(elapsed, event["guest_duration_s"], abs_tol=1e-8), "command duration altered")
        duration += elapsed
        raw_ends.append(end)
        before, after = block_stats(files[prefix + ".block-before"]), block_stats(files[prefix + ".block-after"])
        need(after["write_ios"] == before["write_ios"] and after["write_sectors"] == before["write_sectors"],
             "readonly root image received writes")
        need(after["read_ios"] >= before["read_ios"], "block read counter reversed")
        read_ios += after["read_ios"] - before["read_ios"]
        expected_role = 'B' if sum(x <= index for x in report['boundaries']) % 2 else 'A'
        need(event["role"] == expected_role, "command executed on wrong node")
        need(event["vm"] == 1 and event["tier"] == "cxl", "command ran on wrong memory tier")
    final = files[f"results/{len(commands) - 1:03d}.out"].splitlines(keepends=True)
    markers = [i for i, line in enumerate(final) if line.strip() == SUBMIT]
    need(len(markers) == 1, "submission marker absent/duplicate")
    submitted = fingerprint(b"".join(line for i, line in enumerate(final) if i != markers[0]))
    need(all(submitted[key] == trace["submission"][key] for key in
             ("without_startup_warning_sha256", "startup_warning_count")), "final submission differs from source trace")
    need(json.loads(files["results/complete.json"]) == {"completed_commands": len(commands)}, "trace incomplete")
    need(not FATAL_KERNEL.search(files["results/dmesg.txt"].decode(errors="replace")), "fatal guest kernel error")
    mounts = [line.split() for line in files["results/guest-mounts.txt"].decode().splitlines()]
    lower = [r for r in mounts if r[1] == "/lower"]
    upper = [r for r in mounts if r[1] == "/ram"]
    overlay = [r for r in mounts if r[1] == "/newroot"]
    need(len(lower) == 1 and lower[0][0] == "/dev/vda" and lower[0][2] == "ext4"
         and "ro" in lower[0][3].split(","), "lower is not readonly ext4")
    need(len(upper) == 1 and upper[0][2] == "tmpfs" and len(overlay) == 1
         and overlay[0][2] == "overlay" and "upperdir=/ram/upper" in overlay[0][3].split(","), "private tmpfs upper absent")
    need(not any(r[2] in ("nfs", "nfs4") for r in mounts), "guest unexpectedly uses NFS")
    identities = []
    for item in report["control_commands"]:
        if item.get("receipt", {}).get("event") == "trace_identity":
            identities.append(guest(e, item, "trace_identity"))
    need(identities, "no guest process/FD identity evidence")
    boot, pid, inode = (identities[0][k] for k in ("boot_id", "pid", "fd_inode"))
    need((boot, pid) == (ready["boot_id"], ready["pid"]) and ready["command_count"] == len(commands), "boot workload identity")
    need([row["fd_ordinal"] for row in identities] == list(range(len(identities))), "FD observation sequence reset/gap")
    for identity in identities:
        need((identity["boot_id"], identity["pid"], identity["fd_inode"]) == (boot, pid, inode)
             and identity["fd_deleted"] is True and inode > 0, "guest process or open FD replaced")
        ordinal = identity["fd_ordinal"]
        need(identity["fd_line"] == f"FD-{boot}-{ordinal}" and identity["fd_position"] ==
             sum(len(f"FD-{boot}-{i}\n".encode()) for i in range(ordinal + 1)), "FD contents/position changed")
        complete = identity["next_index"]
        need(0 <= complete <= len(commands), "identity progress range")
        state = f"{boot} {complete} {raw_ends[complete - 1]['output_sha256']}\n" if complete else f"{boot} 0\n"
        need(identity["sentinel_sha256"] == sha(state.encode()), "mutable trace state changed")
    final_hash = raw_ends[-1]['output_sha256']
    need(files["results/state"] == f"{boot} {len(commands)} {final_hash}\n".encode(), "final guest state")
    need(math.isclose(duration, report["guest_duration_s"], abs_tol=1e-8), "aggregate command duration changed")
    return {"commands": len(commands), "returncodes": [r["rc"] for r in raw_ends], "submission": submitted,
            'guest_cpu_flags_sha256': sha(files['results/cpu-flags.txt']) if 'results/cpu-flags.txt' in files else None,
            "tests": tests, "guest_command_seconds": duration, "block_read_ios": read_ios,
            "remote_nfs_reads": remote_reads, "boot_id": boot, "identity_observations": len(identities)}

def integer(value, name, limit=(1 << 64) - 1):
    need(type(value) is int and 0 <= value <= limit, "invalid integer " + name)
    return value

class Evidence:
    def __init__(self, report, root):
        self.report, self.root, self.items, self.order = report, Path(root), {}, {}
        for index, entry in enumerate(report["receipts"]):
            name = entry["file"]
            need(Path(name).name == name and name not in self.items, "duplicate/unsafe receipt")
            self.items[name], self.order[name] = entry, index
            self.read(name)

    def read(self, name):
        need(name in self.items, "unregistered evidence " + name)
        data = (self.root / name).read_bytes()
        need(sha(data) == self.items[name]["sha256"], "receipt hash mismatch " + name)
        return data

    def text(self, name):
        return self.read(name).decode()

    def json(self, name):
        return json.loads(self.read(name))

def handle(raw, memory):
    need(raw['version'] == 4 and not raw.get('mappings') and not raw.get('credits'), 'full mapping in normal export')
    h = raw['handle']
    need(h['abi'] == 4 and h['count']*PAGE == memory and h['state'] in (1, 2)
         and h['owner_epoch'] == int(raw['run_epoch'])
         and h['allocation_generation'] == raw['allocation_generation'] > 0
         and h['phys_base'] == raw['phys_base'] and h['device_bytes'] == raw['device_bytes']
         and h['exhausted'] == 0 and h['reserved'] == [0, 0, 0, 0], 'invalid ownership handle')
    return h

def placement(e, name, memory, requested):
    value = e.json(name)
    h = handle(value, memory)
    private = set()
    mappings = requested['mappings']
    need(len(mappings) == memory // PAGE and requested['allocation_generation'] == h['allocation_generation'],
         'promotion coverage/lease')
    for i, page in enumerate(mappings):
        offset = integer(page['offset'], 'mapping offset')
        need(set(page) == {'gpa', 'offset'} and page['gpa'] == i*PAGE
             and offset % PAGE == 0 and PRIVATE_START <= offset < PRIVATE_END
             and offset not in private, 'promotion alias/geometry')
        private.add(offset)
    return value, private

def verify_cache_receipts(report, root):
    """Replay saved page sets independently of the online oracle and VMM."""
    rows=report.get('oracle',report.get('dirty_oracle',[]))
    need(rows and all(x.get('coverage_verified') or x['phase'] in (6,7,8) for x in rows), 'Missing cache coverage receipts')
    records, consumed={},{}
    pending=0
    demoted=0
    tier_baseline,tier_expected={},{}
    tier_hash_checks=0
    for row in rows:
        if row["phase"] in (6,7,8):
            need(row["missing_pages"]==0,"Tier oracle reported an omission")
            bits=(root/"dirty-oracle"/f"{row['sequence']}.bitmap").read_bytes()
            if row.get('tier_hash_format')==1:
                import zlib
                def hashes(kind):
                    data=zlib.decompress((root/'dirty-oracle'/f"{row['sequence']}.{kind}-hashes.z").read_bytes())
                    need(len(data)==512*32,'Tier page hash geometry')
                    return data
                key=row['node'],row['chunk']
                if row['phase']==6:
                    actual=hashes('dram')
                    need(actual==hashes('home'),'Promotion copy differs from home')
                    tier_baseline[key]=actual
                elif row['phase']==7:
                    actual=hashes('dram');before=tier_baseline[key]
                    dirty=int.from_bytes(bits,'little')
                    need(all(before[i*32:(i+1)*32]==actual[i*32:(i+1)*32] or dirty>>i&1
                             for i in range(512)),'Changed DRAM page omitted from cumulative bitmap')
                    tier_expected[key]=actual
                else:
                    need(hashes('home')==tier_expected.pop(key),'Demotion home differs from DRAM')
                    del tier_baseline[key]
                tier_hash_checks+=1
            if row["phase"]==8:
                bits=(root/"dirty-oracle"/f"{row['sequence']}.bitmap").read_bytes()
                need(len(bits)==64 and row.get("demotion_home_verified"),"Missing tier proof")
                demoted |= int.from_bytes(bits,"little") << (row["chunk"]*512)
            continue
        need(row['missing_pages']==0 and row['other_changed_pages']==0, 'Oracle reported an omission')
        words=(row['pages']+63)//64
        data=(root/'dirty-oracle'/f"{row['sequence']}.bitmap").read_bytes()
        need(len(data)==words*8*6,'Cache receipt bitmap geometry')
        planes=[int.from_bytes(data[i*words*8:(i+1)*words*8],'little')for i in range(6)]
        bitmap,kernel,device,wb,requested,invalidated=planes
        epoch,node=row['epoch'],row['node']
        need(row['phase'] in (1,2,3,4,5),'Unknown maintenance phase')
        if row['phase'] in (1,2,4):
            if row['phase']==1:
                records.clear();consumed.clear()
                need(bitmap==(1<<row['pages'])-1,'Incomplete full-RAM publication')
            need(epoch==len(records)+1 and bitmap==kernel|device and bitmap & ~wb==0,'Writeback coverage or epoch mismatch')
            need(demoted & ~bitmap==0,'Demotion pages omitted from next record')
            demoted=0
            records[epoch]=bitmap
            consumed[node]=epoch
            pending |= bitmap
            if 'changed_bitmap_sha256' in row:
                changed=(root/'dirty-oracle'/f"{row['sequence']}.changed").read_bytes()
                need(hashlib.sha256(changed).hexdigest()==row['changed_bitmap_sha256']
                     and hashlib.sha256(data).hexdigest()==row['bitmap_sha256'],'Altered oracle page sets')
                need(int.from_bytes(changed,'little') & ~pending==0,'Changed GPA omitted from accumulated published sets')
                checked=root/'dirty-oracle'/f"{row['sequence']}.checked"
                if checked.exists(): need(int.from_bytes(checked.read_bytes(),'little')==pending,'Altered accumulated list')
                pending=0
        else:
            start=row['inv_from']
            need(start<=consumed.get(node,0) and row['inv_to']==epoch,'Skipped unconsumed epoch')
            expected=(1<<row['pages'])-1 if epoch-start>8 else 0
            if epoch-start<=8:
                for e in range(start+1,epoch+1):expected|=records[e]
            need(requested==bitmap==expected and expected & ~invalidated==0,'Incomplete receiver invalidation')
            consumed[node]=epoch
    return {'tier_hash_checks':tier_hash_checks,'events':len(rows),'exports':sum(r['phase']==2 for r in rows),
            'imports':sum(r['phase']==3 for r in rows),
            'cleans':sum(r['phase'] in (4,) for r in rows),
            'preinvalidations':sum(r['phase']==5 for r in rows),'missing_pages':0,'coverage_verified':True}

def verify_ept_trace(path):
    report=json.loads(path.read_text())
    need(report['result']=='passed' and report['mode']=='ept-adopt','EPT trace did not finish')
    verify_sources(report,path.parent)
    evidence=Evidence(report,path.parent)
    promotions=[row for row in report['transitions'] if row['kind']=='promotion']
    need(len(promotions)==1,'Expected one DRAM-to-CXL promotion')
    promotion=promotions[0]
    placement(evidence,promotion['export']['file'],report['arguments']['memory_mib']*MIB,
              evidence.json(promotion['operation']['plan_file']))
    identity_pair(evidence,promotion['before'],promotion['after'])
    result={'result':'passed','trace':verify_trace(evidence,report)}
    rows=report.get('oracle',[])
    previous=None;proofs=0
    for row in rows:
        need(row.get('missing_pages',0)==0 and row.get('other_changed_pages',0)==0,'EPT oracle found missing pages')
        if row['phase']==2:previous=row['ept_sha256']
        if row['phase']==3:
            need(previous is not None and previous==row['ept_sha256'] and row['ept_hash_unchanged'],
                 'EPT table changed on adoption')
            proofs+=1;previous=None
    if rows and report['arguments'].get('cache_maintenance',True):result['cache_receipts']=verify_cache_receipts(report,path.parent)
    result.update(oracle_boundaries=len(rows),missing_pages=0,ept_hash_proofs=proofs)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report",type=Path)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    quiet=Path(__file__).resolve().parents[2]/".codex-runs/quiet.lock"
    with quiet.open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        result=verify_ept_trace(args.report)
    data=json.dumps(result,indent=2)+"\n"
    if args.output:args.output.write_text(data)
    print(data,end="")

if __name__=="__main__":main()
