"""Controller-private physical allocation ownership for guest-authorized pooling.

Bloom memory and guest metadata never authorize physical allocation. Grants
remain allocated until the VM proves it no longer maps them. Unused CoW credits
are counted as allocated memory, not reported as free capacity.
"""
from dataclasses import dataclass

from guest_pool_protocol import PAGE, require


@dataclass
class Grant:
    owner: str
    generation: int
    frames: set


def offset_ranges(offsets):
    result = []
    for offset in sorted(offsets):
        if result and result[-1]["offset"] + result[-1]["pages"] * PAGE == offset:
            result[-1]["pages"] += 1
        else:
            result.append({"offset": offset, "pages": 1})
    return result


class FrameAllocator:
    def __init__(self, start, end):
        require(0 <= start < end and (start | end) % PAGE == 0, "invalid allocator bounds")
        self.start, self.end = start, end
        self.free_ranges = [(start, end)]
        self.free_pages = (end-start)//PAGE
        self.grants = {}
        self.generation = 0
        self.events = []

    def allocate(self, owner, pages):
        require(type(pages) is int and 0 <= pages <= self.free_pages, "CXL private capacity exhausted")
        frames, remaining = [], pages
        while remaining:
            start, end = self.free_ranges[0]
            take = min(remaining, (end-start)//PAGE)
            stop = start + take*PAGE
            frames.extend(range(start, stop, PAGE))
            if stop == end:
                self.free_ranges.pop(0)
            else:
                self.free_ranges[0] = (stop, end)
            remaining -= take
        self.free_pages -= pages
        if owner not in self.grants:
            self.generation += 1
            self.grants[owner] = Grant(owner, self.generation, set())
        grant = self.grants[owner]
        grant.frames.update(frames)
        self.events.append({"operation": "allocate", "owner": owner, "generation": grant.generation,
                            "pages": pages, "ranges": offset_ranges(frames)})
        return frames

    def transfer(self, owner, new_owner):
        require(owner in self.grants and new_owner not in self.grants, "invalid ownership transfer")
        grant = self.grants.pop(owner)
        grant.owner = new_owner
        self.grants[new_owner] = grant
        self.events.append({"operation": "transfer", "source_owner": owner, "owner": new_owner,
                            "generation": grant.generation, "pages": len(grant.frames)})

    def release(self, owner, *, fenced, detached):
        require(fenced is True and detached is True, "cannot release live/unfenced CXL backing")
        require(owner in self.grants, "unknown or already released owner")
        grant = self.grants.pop(owner)
        returned = offset_ranges(grant.frames)
        ranges = sorted(self.free_ranges + [(r["offset"], r["offset"] + r["pages"]*PAGE) for r in returned])
        merged = []
        for start, end in ranges:
            require(not merged or merged[-1][1] <= start, "double-free CXL allocation")
            if merged and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        self.free_ranges = merged
        self.free_pages += len(grant.frames)
        self.events.append({"operation": "release", "owner": owner, "generation": grant.generation,
                            "pages": len(grant.frames), "ranges": returned,
                            "fenced": True, "detached": True})
        return sorted(grant.frames)

    def check(self):
        # Explicit diagnostic only; never run on allocate/transfer/release.
        seen = {p for start, end in self.free_ranges for p in range(start, end, PAGE)}
        require(len(seen) == self.free_pages, "free extent accounting mismatch")
        for grant in self.grants.values():
            require(not (seen & grant.frames), "cross-VM private frame alias")
            seen.update(grant.frames)
        require(seen == set(range(self.start, self.end, PAGE)), "allocator lost/out-of-range frames")

    def summary(self):
        return {"capacity_bytes": self.end - self.start, "free_bytes": self.free_pages * PAGE,
                "allocated_bytes": sum(len(g.frames) * PAGE for g in self.grants.values()),
                "owners": {owner: {"generation": g.generation, "allocated_bytes": len(g.frames) * PAGE}
                           for owner, g in sorted(self.grants.items())}}


def promotion_layout(allocator, owner, memory_bytes, shared_pages, credit_pages):
    require(memory_bytes > 0 and memory_bytes % PAGE == 0, "invalid promotion RAM bytes")
    require(owner not in allocator.grants, "owner already has a promotion allocation")
    by_gpa = {}
    for page in shared_pages:
        gpa, offset = page["gpa"], page["pool_offset"]
        require(type(gpa) is int and type(offset) is int and (gpa | offset) % PAGE == 0
                and 0 <= gpa < memory_bytes and gpa not in by_gpa, "invalid shared promotion page")
        require(not allocator.start <= offset < allocator.end, "canonical overlaps private allocator")
        by_gpa[gpa] = offset
    private_count = memory_bytes // PAGE - len(by_gpa)
    grant = allocator.allocate(owner, private_count + credit_pages)
    private = iter(grant[:private_count])
    mappings = [{"gpa": gpa, "offset": by_gpa[gpa] if gpa in by_gpa else next(private),
                 "shared": gpa in by_gpa} for gpa in range(0, memory_bytes, PAGE)]
    return {"mappings": mappings, "credits": grant[private_count:],
            "private_bytes": private_count * PAGE, "shared_bytes": len(by_gpa) * PAGE,
            "credit_bytes": credit_pages * PAGE, "allocation_generation": allocator.grants[owner].generation}


def validate_export(allocator, owner, manifest, memory_bytes, canonical_start, canonical_bytes):
    """Reconcile the current kernel table (including CoW) with its entire grant."""
    require(owner in allocator.grants, "export without allocation owner")
    require(manifest.get("allocation_generation") == allocator.grants[owner].generation,
            "stale manifest allocation generation")
    if manifest.get("version") == 2:
        h = manifest["handle"]
        require(h["abi"] == 2 and h["count"] * PAGE == memory_bytes
                and h["allocation_generation"] == allocator.grants[owner].generation
                and h["owner_epoch"] == int(manifest["run_epoch"])
                and h["state"] in (1, 2), "invalid persistent ownership handle")
        require(not manifest.get("mappings") and not manifest.get("credits"),
                "normal export unexpectedly contains full mapping")
        require(0 <= h["shared_pages"] <= h["count"] and h["credit_count"] >= 0,
                "invalid mapper accounting")
        require(h["count"] - h["shared_pages"] + h["credit_count"] == len(allocator.grants[owner].frames),
                "mapper accounting differs from allocation lease")
        return {"logical_bytes": memory_bytes,
                "private_mapped_bytes": (h["count"] - h["shared_pages"]) * PAGE,
                "shared_mapping_bytes": h["shared_pages"] * PAGE,
                "unused_credit_bytes": h["credit_count"] * PAGE,
                "allocated_private_bytes": len(allocator.grants[owner].frames) * PAGE,
                "proof": "kernel-owned persistent mapping handle"}
    mappings, credits = manifest["mappings"], manifest["credits"]
    require(len(mappings) == memory_bytes // PAGE, "export omits guest RAM pages")
    private, shared = set(), set()
    for index, page in enumerate(mappings):
        require(page["gpa"] == index * PAGE and type(page["shared"]) is bool,
                "manifest GPA order or flags invalid")
        offset = page["offset"]
        require(type(offset) is int and offset % PAGE == 0, "unaligned manifest frame")
        if page["shared"]:
            require(canonical_start <= offset and offset + PAGE <= canonical_start + canonical_bytes,
                    "manifest shared page outside frozen canonical")
            shared.add(offset)
        else:
            require(offset not in private, "private alias within a VM")
            private.add(offset)
    require(all(type(x) is int and x % PAGE == 0 for x in credits)
            and len(set(credits)) == len(credits) and not (private & set(credits)),
            "duplicate, mapped or unaligned unused CoW credit")
    require(private | set(credits) == allocator.grants[owner].frames,
            "kernel manifest and controller allocation ownership differ")
    return {"logical_bytes": memory_bytes, "private_mapped_bytes": len(private) * PAGE,
            "shared_mapping_bytes": sum(p["shared"] for p in mappings) * PAGE,
            "unique_shared_bytes": len(shared) * PAGE, "unused_credit_bytes": len(credits) * PAGE,
            "allocated_private_bytes": len(allocator.grants[owner].frames) * PAGE}


def validate_handoff_handle(source, destination):
    """O(1) controller evidence; kernel import is the mapping authority."""
    a, b = source["handle"], destination["handle"]
    for key in ("abi", "phys_base", "device_bytes", "metadata_offset", "metadata_bytes",
                "cookie_lo", "cookie_hi", "allocation_generation", "count", "full_scan_pages"):
        require(a[key] == b[key], "Handoff changed persistent mapper " + key)
    require(a["state"] == 2 and b["state"] == 1 and b["owner_epoch"] == a["owner_epoch"] + 1,
            "Handoff did not consume sealed ownership epoch")
    # Restoring device queues may perform legitimate CoW after kernel attach.
    require(b["generation"] >= a["generation"], "Mapping generation went backwards")
