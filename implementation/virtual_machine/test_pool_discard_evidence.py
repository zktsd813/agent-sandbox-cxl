#!/usr/bin/env python3
"""Mutate a successful real arm and require independent replay to reject it.

Original experiment files are never modified. Changed raw receipts receive
matching hashes so relational checks, rather than only hash checks, are tested.
"""
import argparse
import copy
import json
from pathlib import Path
import tempfile

from verify_pool_discard import verify_arm, file_sha, sha


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report",type=Path)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    original=json.loads(args.report.read_text())
    verify_arm(original,args.report.parent)
    outcomes=[]
    with tempfile.TemporaryDirectory(prefix="discard-evidence-negatives-",dir="/tmp") as temporary:
        root=Path(temporary)
        for path in args.report.parent.iterdir():
            if path.name != "report.json": (root/path.name).symlink_to(path.resolve(),target_is_directory=path.is_dir())
        changed=[]

        def write(report,name,data,rehash=True):
            target=root/name
            if target.is_symlink(): target.unlink()
            target.write_bytes(data)
            changed.append(name)
            if rehash:
                next(r for r in report["receipts"] if r["file"]==name)["sha256"]=sha(data)

        def changed_pfn(report):
            sample=report["samples"][0]
            old={int(f[1]):int(f[3]) for line in (root/sample["before"]["pfns_file"]).read_text().splitlines()
                 if (f:=line.split())}
            name=sample["anon"]["pfns_file"]
            rows=[list(map(int,line.split())) for line in (root/name).read_text().splitlines()]
            row=next(row for row in rows if row[1] in old)
            row[3]=old[row[1]];row[4]=(row[4]&~((1<<55)-1))|row[3]
            write(report,name,"".join(" ".join(map(str,row))+"\n" for row in rows).encode())

        def failed_guest(report):
            row=report["commands"][0];label=row["receipt"]["label"];name=row["file"]
            lines=(root/name).read_text().splitlines()
            for index,line in enumerate(lines):
                if line.startswith('{"event":"pool_guest"'):
                    value=json.loads(line)
                    if value["label"]==label:
                        value["ok"]=False;row["receipt"]=value
                        lines[index]=json.dumps(value,separators=(",",":"))
            write(report,name,("\n".join(lines)+"\n").encode())

        def host_mutation(report,kind):
            sample=report["samples"][0];vm=str(sample["vm"]);epoch=str(sample["epoch"])
            target=sample["waited"]["after_batch"]+1
            touched=0
            for receipt in report["receipts"]:
                name=receipt["file"]
                if "discard-events" not in name and "-log" not in name: continue
                text=(root/name).read_text();lines=[];modified=False
                for line in text.splitlines():
                    if "POOL_DISCARD_JSON " in line:
                        prefix,raw=line.split("POOL_DISCARD_JSON ",1)
                        # Console command receipts may echo the grep pattern.
                        # They are not host JSON event records.
                        if not raw.lstrip().startswith("{"):
                            lines.append(line)
                            continue
                        value=json.loads(raw)
                        if value["vm_id"]==vm and value["run_epoch"]==epoch and value["batch"]==target:
                            if kind=="missing_ack" and value["phase"]=="ack":
                                modified=True;continue
                            if kind=="reordered_ack":
                                value["phase"]="ack" if value["phase"]=="mapped" else "mapped"
                                modified=True;line=prefix+"POOL_DISCARD_JSON "+json.dumps(value)
                            if kind=="fabricated_discard_range":
                                value["discarded"]=[dict(gpa=512*1024*1024,bytes=4096)]
                                value["discarded_pages"]=1
                                modified=True;line=prefix+"POOL_DISCARD_JSON "+json.dumps(value)
                    lines.append(line)
                if modified:
                    write(report,name,("\n".join(lines)+"\n").encode());touched+=1
            if not touched: raise RuntimeError("negative mutation found no host batch")

        cases=[
            ("top_level_success_cannot_mask_failed_guest",failed_guest),
            ("dropped_sample",lambda r:r["samples"].pop()),
            ("first_fill_timer_not_raw",lambda r:r["samples"][0]["filled"]["receipt"].__setitem__("elapsed_ns",1)),
            ("original_canonical_pfn_reused",changed_pfn),
            ("wrong_runtime_sha",lambda r:r["configuration"].__setitem__("expected_binary_sha256","0"*64)),
            ("receipt_corruption",lambda r:write(r,r["samples"][0]["filled"]["file"],b"corrupt\n",False)),
        ]
        if original["mode"]=="reporting":
            cases += [
                ("fabricated_detached_count",lambda r:r["samples"][0].__setitem__("detached_then_reused_pages",1)),
                ("fabricated_discard_range",lambda r:host_mutation(r,"fabricated_discard_range")),
                ("missing_host_ack",lambda r:host_mutation(r,"missing_ack")),
                ("reordered_host_ack",lambda r:host_mutation(r,"reordered_ack")),
            ]
        for name,mutate in cases:
            for filename in set(changed):
                target=root/filename;target.unlink();target.symlink_to((args.report.parent/filename).resolve())
            changed.clear()
            report=copy.deepcopy(original);mutate(report)
            (root/"report.json").write_text(json.dumps(report))
            try:
                verify_arm(report,root)
            except (ValueError,KeyError) as exc:
                outcomes.append(dict(case=name,result="rejected",reason=str(exc)))
                args.out.write_text(json.dumps(dict(result="running",cases=outcomes),indent=2)+"\n")
            else:
                raise RuntimeError("verifier accepted mutated evidence: "+name)
    result=dict(result="passed",report_sha256=file_sha(args.report),mode=original["mode"],
                cases=outcomes,source_sha256=file_sha(Path(__file__)),
                verifier_sources={name:file_sha(Path(__file__).with_name(name)) for name in
                                  ("verify_pool_discard.py","verify_pool_discard_protocol.py","verify_pool_compare.py")})
    args.out.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__": main()
