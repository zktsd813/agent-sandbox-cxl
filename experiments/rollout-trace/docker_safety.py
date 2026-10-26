"""Guarded Docker operations for a shared host.

The Docker image store is shared by every user of the server, so nothing in
the trace tools may delete an image or container it did not create. All
removals go through this module:

- `snapshot()` records every image ID present before a session starts.
- `pull()` records each image we pull in pull_log.csv (digest, sizes).
- `remove_image()` deletes an image only if it was absent from the
  snapshot, appears in our pull log, no container (anyone's, any state)
  uses it, and it carries no tag we did not pull. It never forces.
- `remove_containers()` deletes only containers labelled with our run id.

There is deliberately no prune.
"""

import csv
import json
import shutil
import time
from pathlib import Path

import docker

LABEL_ROLLOUT = "trace.rollout"
LABEL_RUN = "trace.run"


class Unsafe(Exception):
    pass


class DockerSafety:
    def __init__(self, state_dir, run_id, min_free_gb=150):
        self.client = docker.from_env()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.min_free_gb = min_free_gb
        self.baseline_path = self.state_dir / "baseline_images.json"
        self.pull_log = self.state_dir / "pull_log.csv"
        self.root_dir = self.client.info()["DockerRootDir"]

    # ---- bookkeeping -------------------------------------------------
    def snapshot(self):
        """Record the image IDs present now. Never overwrites an existing
        snapshot, so a restarted session keeps the original baseline."""
        if self.baseline_path.exists():
            return self.baseline()
        ids = sorted(i.id for i in self.client.images.list(all=True))
        self.baseline_path.write_text(json.dumps({"time": time.time(), "ids": ids}))
        return set(ids)

    def baseline(self):
        if not self.baseline_path.exists():
            raise Unsafe("no baseline snapshot; call snapshot() before pulling")
        return set(json.loads(self.baseline_path.read_text())["ids"])

    def pulled(self):
        """{image_id: set(refs)} for images this run pulled itself."""
        out = {}
        if self.pull_log.exists():
            with self.pull_log.open() as f:
                for row in csv.DictReader(f):
                    if row["preexisting"] == "0":
                        out.setdefault(row["image_id"], set()).add(row["ref"])
        return out

    # ---- pull --------------------------------------------------------
    def free_gb(self):
        return shutil.disk_usage(self.root_dir).free / 1e9

    def pull(self, ref):
        """Pull `ref` (repo:tag or repo@digest) and log it. Refuses if the
        shared Docker disk would drop below min_free_gb."""
        base = self.baseline()
        free = self.free_gb()
        if free < self.min_free_gb:
            raise Unsafe(f"Docker root has {free:.0f} GB free < {self.min_free_gb} GB")
        try:
            img = self.client.images.get(ref)
            already = True
        except docker.errors.ImageNotFound:
            already = False
        t0 = time.time()
        if not already:
            img = self.client.images.pull(ref)
        dt = time.time() - t0
        digest = (img.attrs.get("RepoDigests") or [""])[0]
        # An image that was in the baseline, or that we found already
        # present, belongs to someone else (or to nobody we know): never
        # mark it as ours.
        preexisting = already or img.id in base
        new = not self.pull_log.exists()
        with self.pull_log.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "run", "ref", "image_id", "digest", "size_bytes",
                            "pull_s", "preexisting", "free_gb_after"])
            w.writerow([f"{t0:.3f}", self.run_id, ref, img.id, digest, img.attrs["Size"],
                        f"{dt:.2f}", int(preexisting), f"{self.free_gb():.1f}"])
        return img

    # ---- removal -----------------------------------------------------
    def remove_containers(self):
        """Remove containers carrying this run's label. Returns names."""
        removed = []
        for c in self.client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={self.run_id}"}):
            # Double-check the label on the object itself, not just the filter.
            if c.labels.get(LABEL_RUN) != self.run_id:
                continue
            c.remove(force=True)
            removed.append(c.name)
        return removed

    def check_removable(self, ref):
        img = self.client.images.get(ref)
        if img.id in self.baseline():
            raise Unsafe(f"{ref} was present before this session")
        mine = self.pulled()
        if img.id not in mine:
            raise Unsafe(f"{ref} is not in our pull log")
        foreign_tags = set(img.tags) - mine[img.id]
        if foreign_tags:
            raise Unsafe(f"{ref} also carries tags we did not pull: {sorted(foreign_tags)}")
        users = self.client.containers.list(all=True, filters={"ancestor": img.id})
        if users:
            raise Unsafe(f"{ref} is used by containers {[c.name for c in users]}")
        return img

    def remove_image(self, ref):
        """Remove an image we pulled. Returns True if removed, False if a
        guard kept it (the reason is logged, never raised)."""
        try:
            img = self.check_removable(ref)
        except (Unsafe, docker.errors.ImageNotFound) as e:
            self._log_removal(ref, False, str(e))
            return False
        try:
            # No force: Docker itself refuses if anything still uses it.
            self.client.images.remove(img.id, force=False, noprune=False)
        except docker.errors.APIError as e:
            self._log_removal(ref, False, f"docker refused: {e.explanation}")
            return False
        self._log_removal(ref, True, "")
        return True

    def _log_removal(self, ref, ok, why):
        with (self.state_dir / "removal_log.jsonl").open("a") as f:
            f.write(json.dumps({"time": time.time(), "ref": ref, "removed": ok, "why": why}) + "\n")
