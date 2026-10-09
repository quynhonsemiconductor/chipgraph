"""S3 spike: Edalize `Sim(...).configure()` starts no subprocess and is deterministic.

    python probe_configure.py --out DIR

For the `verilator` and `icarus` tools with cocotb on: configures the same EDAM into two
work roots with `subprocess.Popen` spied on, then compares the files written. This is
what makes "Edalize in-process for configure, the Runner for make and the simulator"
possible for M2-05. Exits 1 if a subprocess was started or the outputs differ.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import s3_common


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    edams = {t: s3_common.edam(t, s3_common.read_filelist()) for t in ("verilator", "icarus")}

    calls: list[object] = []
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):
        def __init__(self, *a: object, **k: object) -> None:
            calls.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    subprocess.Popen = SpyPopen
    try:
        from edalize.flows.sim import Sim

        ok = True
        for tool, the_edam in edams.items():
            digests = []
            for i in (1, 2):
                work = args.out.resolve() / f"{tool}-{i}"
                shutil.rmtree(work, ignore_errors=True)
                work.mkdir(parents=True)
                Sim(the_edam, work).configure()
                digests.append(
                    {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir()}
                )
            same = digests[0] == digests[1]
            ok &= same
            print(f"{tool}: wrote {sorted(digests[0])}; identical across work roots: {same}")
    finally:
        subprocess.Popen = real_popen

    print(f"subprocesses started by configure(): {calls or 'none'}")
    return 0 if ok and not calls else 1


if __name__ == "__main__":
    sys.exit(main())
