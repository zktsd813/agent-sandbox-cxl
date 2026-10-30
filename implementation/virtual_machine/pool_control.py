"""Controller for paused, immutable block pooling in the disposable CXL lab.

Bloom slots are shared fixed-size hints, including for DRAM VMs. Exact indexes
are private controller records obtained from each owner's VMM, never guest
inode/folio pointers. A Bloom result cannot authorize a mapping: PoolCache
checks its own current bytes against frozen canonical CXL bytes before remap.
The canonical image is prefilled once, never rewritten/reused until all VMs
exit. This prototype measures shared mappings, not released DAX reservations.
"""
import json
import hashlib
import shlex

PAGE = 4096
SLOT_BYTES = 65536


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


class PoolControl:
    def __init__(self, controller):
        self.c = controller
        self.index = {}
        # The allocator/controller owns this exact READY catalog. It is separate
        # from owner observation metadata and never resides in shared CXL.
        self.ready_keys = set()
        self.serial = 0
        self.events = []

    def setup(self):
        c = self.c
        require(c.pool_offset + c.image_bytes < c.bloom_offset,
                "canonical image overlaps Bloom slots")
        raw, receipt = c.shell("A", shlex.join([
            "/lab/pool-dax-copy", "import", "/disk/sandbox.raw", "/dev/dax0.0",
            str(c.pool_offset), str(c.image_bytes)]), "canonical-publish", timeout=240)
        require('"result":"passed"' in raw, "canonical publication failed")
        publication = {"event": "canonical_publish", "file": receipt,
                            "bytes": c.image_bytes, "image_id": c.image_id,
                            "policy": "frozen until all sandbox processes are fenced"}
        self.events.append(publication)
        for sandbox in c.sandboxes:
            self._bloom(sandbox, "zero", [], sandbox.vm)
        return publication

    def _bloom(self, sandbox, operation, keys, slot):
        c = self.c
        self.serial += 1
        prefix = f"/run/pool-bloom-{self.serial:06d}"
        # Keys and resulting hits are bounded integers; no remote pointers.
        c.write_bytes(sandbox.role, prefix + ".keys",
                      "".join(f"{k}\n" for k in sorted(keys)).encode(), "bloom-keys")
        raw, receipt = c.shell(sandbox.role, shlex.join([
            "/lab/pool-bloom-tool", operation, "/dev/dax0.0", str(slot),
            c.image_id, prefix + ".keys", prefix + ".out"]), "bloom-" + operation)
        records = [json.loads(line.removeprefix("POOL_BLOOM_JSON "))
                   for line in raw.splitlines() if line.startswith("POOL_BLOOM_JSON ")]
        require(len(records) == 1, "Bloom receipt absent")
        record = {**records[0], "file": receipt, "reader_vm": sandbox.vm,
                  "reader_role": sandbox.role, "reader_tier": sandbox.tier}
        if operation == "probe":
            data = c.fetch_bytes(sandbox.role, prefix + ".out", "bloom-hits")
            hits = {int(line) for line in data.splitlines()}
            require(hits <= set(keys), "Bloom returned an unrequested key")
            require(len(hits) == record["hits"], "Bloom hit count mismatch")
        else:
            hits = set()
        self.events.append(record)
        return hits, record

    def _eligible(self, offset):
        return any(start <= offset and offset + PAGE <= start + length
                   for start, length in self.c.payload_extents)

    def admit(self, sandbox):
        c = self.c
        empty = {"vm_id": str(sandbox.vm), "run_epoch": str(sandbox.epoch),
                 "pool_path": "/dev/dax0.0", "pages": []}
        c.write_json(sandbox.role, sandbox.plan, empty, "empty-plan")
        c.api(sandbox.role, sandbox.directory, "/actions", "PUT", {"action_type": "PoolCache"})
        observed = c.fetch_json(sandbox.role, sandbox.hints, "owner-hints")
        require(observed["version"] == 1 and observed["vm_id"] == str(sandbox.vm)
                and observed["run_epoch"] == str(sandbox.epoch), "stale hint owner/epoch")
        candidates = [h for h in observed["hints"]
                      if h["image_id"] == c.image_id and self._eligible(h["image_offset"])]
        require(len({h["gpa"] for h in candidates}) == len(candidates), "duplicate GPA hint")
        keys = {h["image_offset"] for h in candidates}
        # Private snapshots of owner observation indexes may themselves be stale;
        # only canonical ready bytes + final paused exact comparison are trusted.
        discovery = []
        discovered = set()
        for owner, private in sorted(self.index.items()):
            if owner == sandbox.vm:
                continue
            hits, receipt = self._bloom(sandbox, "probe", keys, owner)
            # Consult the owner-specific index only for positive summary keys.
            exact = {key for key in hits if key in private["keys"]}
            discovered |= exact
            discovery.append({"owner_vm": owner, "owner_epoch": private["epoch"],
                              "owner_tier": private["tier"], "bloom_hits": len(hits),
                              "exact_candidate_hits": len(exact),
                              "false_positive_candidates": len(hits-exact),
                              "bloom_receipt": receipt})
        self.index[sandbox.vm] = {"keys": keys, "epoch": sandbox.epoch,
                                  "tier": sandbox.tier, "role": sandbox.role}
        _, published = self._bloom(sandbox, "build", keys, sandbox.vm)
        plan = dict(empty)
        if sandbox.tier == "cxl":
            # A first observation may admit a new canonical entry; a peer hit
            # reuses its ready entry. Both require identical final validation.
            plan["pages"] = [{"gpa": h["gpa"], "image_id": c.image_id,
                              "image_offset": h["image_offset"],
                              "pool_offset": c.pool_offset + h["image_offset"]}
                             for h in candidates
                             if h["image_offset"] in discovered
                             or h["image_offset"] not in self.ready_keys]
        c.write_json(sandbox.role, sandbox.plan, plan, "pool-plan")
        c.api(sandbox.role, sandbox.directory, "/actions", "PUT", {"action_type": "PoolCache"})
        result = c.fetch_json(sandbox.role, sandbox.report, "pool-admission")
        missed_ready = len((keys & self.ready_keys) - discovered)
        by_gpa = {p["gpa"]: p["image_offset"] for p in plan["pages"]}
        self.ready_keys.update(by_gpa[p["gpa"]] for p in result["mapped_pages"])
        return {"vm": sandbox.vm, "tier": sandbox.tier, "role": sandbox.role,
                "epoch": sandbox.epoch, "observed_candidates": len(candidates),
                "unique_keys": len(keys), "discovered_peer_keys": len(discovered),
                "retained_private_after_hint_miss": missed_ready,
                "candidate_bytes": len(candidates)*PAGE, "bloom": published,
                "discovery": discovery, "vmm": result,
                "vmm_file": c.receipts[-1]["file"],
                "plan_pages": plan["pages"]}

    def approximate_negative_tests(self, reader, owner):
        """Inject stale negatives and positives; discovery is never authority."""
        keys = set(self.index[owner.vm]["keys"])
        require(keys, "negative test needs real observed candidates")
        sample = sorted(keys)[::max(1, len(keys)//256)]
        self._bloom(owner, "zero", [], owner.vm)
        absent, zero = self._bloom(reader, "probe", sample, owner.vm)
        require(not absent, "zero summary must miss existing candidates")
        self._bloom(owner, "ones", [], owner.vm)
        # Out-of-image offsets cannot be eligible exact keys, regardless of bits.
        missing = [self.c.image_bytes + PAGE*i for i in range(1, 258)]
        hits, ones = self._bloom(reader, "probe", missing, owner.vm)
        require(hits == set(missing) and not (hits & keys),
                "false positive must fail private exact lookup")
        self._bloom(owner, "build", keys, owner.vm)
        return {"false_negative_existing_keys": len(sample),
                "false_positive_rejected_keys": len(missing),
                "zero_receipt": zero, "ones_receipt": ones,
                "result": "passed"}

    def audit(self, sandbox, admission, require_shared=True):
        """Verify actual present PFNs after guest reads, not just VMA flags."""
        c = self.c
        if require_shared and sandbox.tier == "cxl" and getattr(c, "cache_gpa_audit", False):
            return self._audit_current_file_cache(sandbox, admission)
        rows = admission["vmm"]["mapped_pages"]
        if sandbox.tier == "dram":
            require(not rows, "strict DRAM stage installed CXL overlays")
            return {"tier": "dram", "mapped_pages": 0}
        self.serial += 1
        prefix = f"/run/pool-audit-{self.serial:06d}"
        inputs = "".join(f"{r['hva']} {r['pool_offset']}\n" for r in rows).encode()
        c.write_bytes(sandbox.role, prefix + ".tsv", inputs, "pfn-rows")
        input_file = None
        if getattr(c, "cache_gpa_audit", False):
            require(c.fetch_bytes(sandbox.role, prefix + ".tsv", "pfn-input") == inputs,
                    "PFN scanner input transfer mismatch")
            input_file = c.receipts[-1]["file"]
        raw, receipt = c.shell(sandbox.role,
            f"read p t < {shlex.quote(sandbox.directory+'/fc.pid')}\n" +
            f"/lab/pool-pfn-audit \"$p\" /dev/dax0.0 {c.pool_offset} {c.image_bytes} " +
            shlex.join([prefix + ".tsv", prefix + ".shared"]), "pfn-audit", timeout=240)
        result = [json.loads(line.removeprefix("POOL_PFN_JSON "))
                  for line in raw.splitlines() if line.startswith("POOL_PFN_JSON ")]
        require(len(result) == 1 and result[0]["rows"] == len(rows), "PFN audit incomplete")
        measured = result[0]
        if require_shared:
            require(measured["shared_bytes"] >= c.minimum_cache_bytes,
                    "actual shared CXL frames below required resident cache size")
        shared = c.fetch_bytes(sandbox.role, prefix + ".shared", "shared-offsets")
        offsets = [int(line) for line in shared.splitlines()]
        require(len(offsets) == measured["shared_pages"], "shared-offset evidence mismatch")
        return {**measured, "file": receipt, "tier": sandbox.tier,
                "admission_file": admission.get("vmm_file"),
                "unique_shared_offsets": len(set(offsets)),
                "canonical_identity": c.image_id,
                "pfn_input_file": input_file, "pfn_input_sha256": hashlib.sha256(inputs).hexdigest(),
                "shared_offsets_file": c.receipts[-1]["file"],
                "shared_offsets": sorted(set(offsets))}

    def audit_group(self, sandboxes, admissions):
        """Freeze the same current-cache observation interval across all VMs."""
        c = self.c
        if not getattr(c, "cache_gpa_audit", False):
            return [self.audit(s, admissions[s.vm]) for s in sandboxes]
        pinned, paused = {}, []
        try:
            for sandbox in sandboxes:
                pinned[sandbox.vm] = c.command_vm(sandbox, "cachemap", require_cache=True)
                c.pause(sandbox)
                paused.append(sandbox)
            _, group_file = c.shell(sandboxes[0].role,
                "echo POOL_CACHE_GROUP_FROZEN " + " ".join(str(s.vm) for s in sandboxes),
                "cache-group-frozen")
            results = []
            for sandbox in sandboxes:
                measured = self._audit_current_file_cache(sandbox, admissions[sandbox.vm],
                                                         pre_pinned=pinned[sandbox.vm])
                measured["frozen_group_file"] = group_file
                results.append(measured)
            return results
        finally:
            for sandbox in paused:
                c.resume(sandbox)
            for sandbox in sandboxes:
                if sandbox.vm in pinned:
                    c.command_vm(sandbox, "cacheunmap", require_cache=True)

    def _audit_current_file_cache(self, sandbox, admission, pre_pinned=None):
        """Guest-assisted measurement only; its output never constructs a plan.

        Bind resident MAP_SHARED file pages to VMM-observed GPA mappings and to
        actual canonical PFNs in one paused interval. Historical freed buffers
        cannot contribute to the measured current-file-cache shared byte count.
        """
        c = self.c
        pinned = pre_pinned or c.command_vm(sandbox, "cachemap", require_cache=True)
        require(pinned["receipt"]["block_read_ios"] == 0 and
                pinned["receipt"]["resident_before"]*PAGE >= c.minimum_cache_bytes,
                "cache correspondence probe fetched previously absent pages")
        if pre_pinned is None:
            c.pause(sandbox)
        try:
            self.serial += 1
            prefix = f"/run/pool-cache-gpas-{self.serial:06d}"
            label = pinned["receipt"]["label"]
            c.shell(sandbox.role,
                    "grep -E " + shlex.quote("^POOL_CACHE_(GPA|RANGE) " + label + " ") + " " +
                    shlex.quote(sandbox.directory + "/fc.log") + " > " + shlex.quote(prefix),
                    "cache-gpa-select")
            data = c.fetch_bytes(sandbox.role, prefix, "cache-gpas")
            cache_file = c.receipts[-1]["file"]
            entries = []
            for line in data.decode().splitlines():
                fields = line.split()
                require(len(fields) in (4, 5) and fields[0] in ("POOL_CACHE_GPA", "POOL_CACHE_RANGE")
                        and fields[1] == label, "guest cache GPA record malformed")
                require((fields[0] == "POOL_CACHE_GPA") == (len(fields) == 4), "cache record shape")
                file_offset, gpa = map(int, fields[2:4])
                count = int(fields[4]) if len(fields) == 5 else 1
                require(0 < count <= c.payload_bytes // PAGE and len(entries)+count <= c.payload_bytes // PAGE,
                        "cache range size outside payload")
                require(file_offset % PAGE == gpa % PAGE == 0 and gpa > 0,
                        "unaligned guest cache GPA record")
                entries.extend((file_offset+i*PAGE, gpa+i*PAGE) for i in range(count))
            require([offset for offset, _ in entries] == list(range(0, c.payload_bytes, PAGE)),
                    "current guest file-cache GPA coverage incomplete")
            require(len({gpa for _, gpa in entries}) == len(entries), "duplicate current guest cache GPA")
            mapped = {p["gpa"]: p for p in admission["vmm"]["mapped_pages"]}
            selected, pool_to_file = [], {}
            for file_offset, gpa in entries:
                cursor = file_offset
                image_offset = None
                for disk_start, length in c.payload_extents:
                    if cursor < length:
                        image_offset = disk_start + cursor
                        break
                    cursor -= length
                require(image_offset is not None, "file offset outside prepared extents")
                row = mapped.get(gpa)
                if row is None or row["pool_offset"] != c.pool_offset + image_offset:
                    continue
                selected.append(row)
                pool_to_file[row["pool_offset"]] = file_offset
            # Reuse the physical scanner on the joined current-file-cache set,
            # not on all VMM history. False keeps this call nonrecursive.
            measured = self.audit(sandbox, {"vmm": {"mapped_pages": selected}}, require_shared=False)
            require(measured["shared_bytes"] >= c.minimum_cache_bytes,
                    "actual current guest file-cache sharing below threshold")
            shared_file_offsets = sorted(pool_to_file[offset] for offset in measured["shared_offsets"])
            return {**measured, "vm": sandbox.vm, "role": sandbox.role, "epoch": sandbox.epoch,
                    "current_file_cache_proven": True, "cache_probe": pinned,
                    "admission_file": admission["vmm_file"],
                    "cache_gpa_file": cache_file, "cache_pages": len(entries),
                    "unmatched_cache_pages": len(entries) - len(selected),
                    "shared_file_offsets": shared_file_offsets}
        finally:
            if pre_pinned is None:
                c.resume(sandbox)
                c.command_vm(sandbox, "cacheunmap", require_cache=True)

    def admission_negative_tests(self, sandbox, admission):
        """Real API rejection checks against a paused, mapped sandbox."""
        import copy
        c = self.c
        require(sandbox.tier == "cxl" and admission["plan_pages"], "needs mapped CXL VM")
        page = copy.deepcopy(admission["plan_pages"][0])
        good = {"vm_id": str(sandbox.vm), "run_epoch": str(sandbox.epoch),
                "pool_path": "/dev/dax0.0", "pages": [page]}
        cases = []
        for name in ("vm", "epoch", "image", "unaligned", "duplicate", "out_of_range"):
            bad = copy.deepcopy(good)
            if name == "vm": bad["vm_id"] = "wrong-owner"
            elif name == "epoch": bad["run_epoch"] = "retired-epoch"
            elif name == "image": bad["pages"][0]["image_id"] = "f"*64 if c.image_id != "f"*64 else "e"*64
            elif name == "unaligned": bad["pages"][0]["gpa"] += 1
            elif name == "duplicate": bad["pages"].append(copy.deepcopy(page))
            elif name == "out_of_range": bad["pages"][0]["pool_offset"] = 1 << 62
            c.write_json(sandbox.role, sandbox.plan, bad, "negative-plan")
            raw, receipt = c.shell(sandbox.role, shlex.join([
                "/lab/shared-probe", "api", sandbox.directory + "/api.sock", "PUT", "/actions",
                json.dumps({"action_type": "PoolCache"}), "400"]), "negative-api")
            responses = [json.loads(line.removeprefix("LAB_API_JSON "))
                         for line in raw.splitlines() if line.startswith("LAB_API_JSON ")]
            require(len(responses) == 1 and responses[0]["status"] == 400, "bad plan accepted: " + name)
            cases.append({"case": name, "file": receipt, "status": 400})
        # Valid syntax, but wrong candidate key / changed canonical bytes. These
        # are best-effort misses; the existing guest mapping must stay intact.
        for name in ("stale_hint", "content_mismatch"):
            bad = copy.deepcopy(good)
            if name == "stale_hint": bad["pages"][0]["image_offset"] += PAGE
            else: bad["pages"][0]["pool_offset"] += PAGE
            c.write_json(sandbox.role, sandbox.plan, bad, "negative-plan")
            c.api(sandbox.role, sandbox.directory, "/actions", "PUT", {"action_type": "PoolCache"})
            result = c.fetch_json(sandbox.role, sandbox.report, "negative-admission")
            field = "rejected_hint" if name == "stale_hint" else "rejected_content"
            require(result["mapped"] == 0 and result[field] == 1, "unsafe candidate admitted: " + name)
            cases.append({"case": name, "report": result})
        c.write_json(sandbox.role, sandbox.plan,
                     {**good, "pages": admission["plan_pages"]}, "restore-valid-plan")
        return {"result": "passed", "cases": cases}
