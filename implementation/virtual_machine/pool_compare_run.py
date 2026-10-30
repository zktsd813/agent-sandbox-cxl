#!/usr/bin/env python3
"""Run isolated, fresh-boot comparison arms and preserve each raw report."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import time

HERE=Path(__file__).resolve().parent
ARMS=("stock-dram","patched-dram-disabled","patched-cxl-hints-unpooled","patched-cxl-pooled","stock-snapshot")


def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):h.update(block)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets",type=Path,required=True)
    p.add_argument("--qemu",type=Path,required=True)
    p.add_argument("--qemu-root",type=Path,required=True)
    p.add_argument("--session",required=True)
    p.add_argument("--rounds",type=int,default=5)
    p.add_argument("--arms",nargs="+",choices=ARMS,default=list(ARMS))
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    assets=a.assets.resolve()
    suite={"schema":"pool-compare-suite-v1","result":"running","rounds":a.rounds,"arms":[],
        "fixture_manifest":json.loads((assets/"manifest.json").read_text()),"source_hashes":{name:digest(HERE/name) for name in
            ("pool_compare_run.py","pool_compare_bench.py","pool_compare_prepare.py","pool_compare_probe.c","pool_compare_pfn.c")}}
    (a.output/"measured-sources").mkdir()
    for name in suite["source_hashes"]:shutil.copy2(HERE/name,a.output/"measured-sources"/name)
    def save():(a.output/"report.json").write_text(json.dumps(suite,indent=2)+"\n")
    save()
    try:
        for i,arm in enumerate(a.arms):
            session=f"{a.session}-{i}"
            disk=a.output/(arm+"-storage.raw")
            subprocess.run(["cp","--sparse=always","--reflink=auto",str(assets/"storage.raw"),str(disk)],check=True)
            env=dict(os.environ,CXL_LAB_ASSETS=str(assets),CXL_LAB_SESSION=session,CXL_LAB_QEMU_ROOT=str(a.qemu_root.resolve()))
            prefix=["taskset","-c","0-7","python3","-u"]
            subprocess.run(prefix+[str(HERE/"lab.py"),"start","--vcpus","4","--memory-mib","8192",
                "--shared-mib","64","--cxl-mib","4096","--dax-guest","--dax-4k","--pool-cow",
                "--migration-network","--network-backend","tap","--memory-backing-dir","/dev/shm",
                "--home-storage-image",str(disk.resolve()),"--qemu",str(a.qemu.resolve())],env=env,check=True)
            run=Path(json.loads((HERE/".local"/f"current-{session}.json").read_text())["run"])
            try:
                subprocess.run(prefix+[str(HERE/"pool_compare_bench.py"),"--arm",arm,"--rounds",str(a.rounds)],env=env,check=True)
            finally:
                subprocess.run(["python3",str(HERE/"lab.py"),"stop"],env=env,check=True)
                reports=list(run.glob("compare-*/report.json"))
                if reports:
                    assert len(reports)==1
                    shutil.copytree(reports[0].parent,a.output/arm)
                    ref={"arm":arm,"report_file":arm+"/report.json","sha256":digest(reports[0]),"original_report":str(reports[0])}
                    if arm=="stock-snapshot":suite["stock_snapshot_proof"]=ref
                    else:suite["arms"].append(ref)
                    save()
            print(f"COMPARE_ARM_DONE {arm}",flush=True)
        suite["result"]="passed"
    except BaseException as exc:
        suite["result"]="failed";suite["error"]=repr(exc);raise
    finally:
        save();print("POOL_COMPARE_SUITE "+str(a.output/"report.json"),flush=True)


if __name__=="__main__":main()
