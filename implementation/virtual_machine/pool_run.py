#!/usr/bin/env python3
"""Launch one disposable pair, run the multi-sandbox pool check, then stop it."""
import argparse
import os
from pathlib import Path
import subprocess

HERE=Path(__file__).resolve().parent


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets",type=Path,required=True)
    p.add_argument("--qemu",type=Path,required=True)
    p.add_argument("--qemu-root",type=Path,required=True)
    p.add_argument("--session",required=True)
    p.add_argument("--cycles",type=int,default=2)
    p.add_argument("--vcpus",type=int,default=1)
    p.add_argument("--io-engine",choices=("Sync","Async"),default="Sync")
    p.add_argument("--host-cpus",default="0-5")
    args=p.parse_args()
    env=dict(os.environ,CXL_LAB_ASSETS=str(args.assets.resolve()),CXL_LAB_SESSION=args.session,
             CXL_LAB_QEMU_ROOT=str(args.qemu_root.resolve()))
    prefix=["taskset","-c",args.host_cpus,"python3","-u"]
    subprocess.run(prefix+[str(HERE/"lab.py"),"start","--vcpus","4","--memory-mib","8192",
        "--shared-mib","64","--cxl-mib","4096","--dax-guest","--dax-4k","--pool-cow",
        "--migration-network","--network-backend","tap","--memory-backing-dir","/dev/shm",
        "--home-storage-image",str(args.assets.resolve()/"storage.raw"),"--qemu",str(args.qemu.resolve())],
        env=env,check=True)
    try:
        subprocess.run(prefix+[str(HERE/"pool_bench.py"),"--cycles",str(args.cycles),"--sandboxes","3",
                               "--memory-mib","512","--minimum-cache-mib","128","--vcpus",str(args.vcpus),
                               "--io-engine",args.io_engine],env=env,check=True)
    finally:
        subprocess.run(["python3",str(HERE/"lab.py"),"stop"],env=env,check=True)


if __name__=="__main__": main()
