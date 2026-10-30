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
        self.free = set(range(start, end, PAGE))
        self.grants = {}
        self.generation = 0
        self.events = []

    def allocate(self, owner, pages):
        require(type(pages) is int and 0 <= pages <= len(self.free), "CXL private capacity exhausted")
        frames = sorted(self.free)[:pages]
        self.free.difference_update(frames)
        if owner not in self.grants:
            self.generation += 1
            self.grants[owner] = Grant(owner, self.generation, set())
        grant = self.grants[owner]
        grant.frames.update(frames)
        self.events.append({"operation": "allocate", "owner": owner, "generation": grant.generation,
                            "pages": pages, "ranges": offset_ranges(frames)})
        self.check()
        return frames

    def transfer(self, owner, new_owner):
        require(owner in self.grants and new_owner not in self.grants, "invalid ownership transfer")
        grant = self.grants.pop(owner)
        grant.owner = new_owner
        self.grants[new_owner] = grant
        self.events.append({"operation": "transfer", "source_owner": owner, "owner": new_owner,
                            "generation": grant.generation, "pages": len(grant.frames)})
        self.check()

    def release(self, owner, *, fenced, detached):
        require(fenced is True and detached is True, "cannot release live/unfenced CXL backing")
        require(owner in self.grants, "unknown or already released owner")
        grant = self.grants.pop(owner)
        require(not (grant.frames & self.free), "double-free CXL allocation")
        self.free.update(grant.frames)
        self.events.append({"operation": "release", "owner": owner, "generation": grant.generation,
                            "pages": len(grant.frames), "ranges": offset_ranges(grant.frames),
                            "fenced": True, "detached": True})
        self.check()
        return sorted(grant.frames)

    def check(self):
        seen = set(self.free)
        for grant in self.grants.values():
            require(not (seen & grant.frames), "cross-VM private frame alias")
            seen.update(grant.frames)
        require(seen == set(range(self.start, self.end, PAGE)), "allocator lost/out-of-range frames")

    def summary(self):
        return {"capacity_bytes": self.end - self.start, "free_bytes": len(self.free) * PAGE,
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
