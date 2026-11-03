#!/usr/bin/env python3
"""Public RL training rollouts (sweagent/iter2-rl-rollouts) as replay input for the contention experiments.

Subcommands:
- index: one pass over the tar shards -> index.jsonl (one row per rollout: group key, image, workdir, sizes, timing).
- groups: rebuild GRPO groups (same run, instance and step) from sample index and file mtime.
- select: random task sample stratified by image family, whole groups only, within a disk budget.
- plan: step plan for synchronous replay that follows the original job (two waves per step, recorded start delays,
  idle between steps, new groups every step).
- convert: write the selected rollouts (or a plan's groups) in the mini-swe-agent .traj.json shape that `replay.load_trace` reads,
  plus candidates-rl.json for `sandbox.py`.

The data is untrusted text; nothing here executes it. Commands are executed only inside the microVMs.
"""

import argparse
import collections
import json
import math
import random
import re
import tarfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = Path("/Serverless/agent-sandbox-cxl/rl-iter2")
RAW = DATA / "raw"
INDEX = DATA / "index.jsonl"
GROUPS = DATA / "groups.json"
SELECTION = DATA / "selection.json"
TRACES = DATA / "traces"
CANDIDATES = HERE / "candidates-rl.json"
RETURNCODE = re.compile(r"<returncode>(-?\d+)</returncode>")
THINK = re.compile(r"<think>(.*?)</think>", re.S)


def family(image):
    image = image.removeprefix("docker.io/")
    return image.split("/")[0].split(":")[0]


def index_shard(path):
    rows = []
    with tarfile.open(path, "r:gz") as tf:
        for member in tf:
            if not (member.isfile() and member.name.endswith(".json")):
                continue
            d = json.load(tf.extractfile(member))
            run, base = member.name.rsplit("/", 1)
            instance_id, sample = base[:-5].rsplit("_", 1)
            inst = d.get("instance") or {}
            msgs = d.get("messages") or []
            commands, gaps, think, visible, chars_before, llm = 0, [], 0, 0, 0, []
            prev_tool = None
            for m in msgs:
                content = m.get("content") or ""
                if m["role"] == "assistant":
                    actions = (m.get("extra") or {}).get("actions") or []
                    commands += len(actions)
                    t = THINK.search(content)
                    tc = len(t.group(1)) if t else 0
                    think += tc
                    visible += len(content) - tc
                    if prev_tool is not None and m.get("timestamp") is not None:
                        gaps.append(m["timestamp"] - prev_tool)
                        llm.append((chars_before, len(content), m["timestamp"] - prev_tool))
                elif m["role"] == "tool":
                    prev_tool = m.get("timestamp")
                chars_before += len(content)
            times = [m["timestamp"] for m in msgs if m.get("timestamp") is not None]
            rows.append({
                "shard": path.name, "member": member.name, "run": run, "instance_id": instance_id,
                "sample": int(sample), "mtime": member.mtime, "image": inst.get("image_url"),
                "family": family(inst.get("image_url") or "none"), "workdir": inst.get("workdir") or "/testbed",
                "pre_commands": bool(inst.get("pre_commands")), "exit_status": d.get("exit_status"),
                "n_steps": d.get("n_steps"), "commands": commands,
                "all_tokens": d.get("all_tokens_length"), "response_tokens": len(d.get("response_token_ids") or []),
                "assistant_chars": think + visible, "think_chars": think, "total_chars": chars_before,
                "first_ts": min(times) if times else None, "last_ts": max(times) if times else None,
                "llm_gap_sum_s": round(sum(gaps), 3), "turns": len(gaps) + 1,
                "env_creation_s": d.get("env_creation_time"),
                # (chars before the turn, chars of the turn's response, observed gap) for the latency fit
                "llm_points": llm,
            })
    return rows


def cmd_index(args):
    shards = sorted(RAW.glob("trajectories_*.tar.gz"))
    with ProcessPoolExecutor(len(shards)) as pool:
        results = list(pool.map(index_shard, shards))
    rewards = {}
    with tarfile.open(RAW / "rewards.tar.gz", "r:gz") as tf:
        for member in tf:
            if member.isfile() and member.name.endswith("_rewards.json"):
                d = json.load(tf.extractfile(member))
                rewards[member.name.rsplit("/", 1)[1][:-len("_rewards.json")]] = d.get("resolved")
    n = 0
    with INDEX.open("w") as out:
        for rows in results:
            for row in rows:
                row["resolved"] = rewards.get(f"{row['instance_id']}_{row['sample']}")
                out.write(json.dumps(row) + "\n")
                n += 1
    print(f"{n} rollouts -> {INDEX}")


def load_index():
    return [json.loads(line) for line in INDEX.open()]


def cmd_groups(args):
    """Same run + instance; split into groups where consecutive sample indices jump by more than `gap`
    or file mtimes differ by more than `window_s` from the group's first member."""
    rows = load_index()
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["run"], r["instance_id"])].append(r)
    groups = []
    for (run, iid), members in by.items():
        members.sort(key=lambda r: (r["sample"], r["mtime"]))
        current = [members[0]]
        for r in members[1:]:
            if r["sample"] - current[-1]["sample"] > args.gap or abs(r["mtime"] - current[0]["mtime"]) > args.window_s:
                groups.append(current)
                current = [r]
            else:
                current.append(r)
        groups.append(current)
    sizes = collections.Counter(len(g) for g in groups)
    dup = sum(len(g) != len({r["sample"] for r in g}) for g in groups)
    out = [{"run": g[0]["run"], "instance_id": g[0]["instance_id"], "image": g[0]["image"], "family": g[0]["family"],
            "members": [f"{r['instance_id']}_{r['sample']}@{r['shard']}" for r in g],
            "samples": [r["sample"] for r in g], "mtime_span_s": max(r["mtime"] for r in g) - min(r["mtime"] for r in g)}
           for g in groups]
    GROUPS.write_text(json.dumps({"rule": {"gap": args.gap, "window_s": args.window_s}, "groups": out}))
    print("groups", len(groups), "size histogram", sorted(sizes.items()), "groups with duplicate sample ids", dup)
    spans = sorted(g["mtime_span_s"] for g in out if len(g["members"]) > 1)
    if spans:
        print("mtime span of multi-member groups p50/p90/max", spans[len(spans) // 2], spans[int(len(spans) * .9)], spans[-1])


def hub_size(image, cache):
    """Compressed size from the Docker Hub tag API (does not count as a pull); uncompressed is estimated with the
    2.6x unpack ratio measured on the 264 swebench images (harness/trace-replay/data/images-disk-complete.json)."""
    if image in cache:
        return cache[image]
    ref = image.removeprefix("docker.io/")
    name, tag = ref.split(":", 1) if ":" in ref else (ref, "latest")
    namespace, repo = name.split("/", 1)
    url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags/{tag}"
    entry = {"compressed_gib": None, "uncompressed_gib": None}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                full = json.load(response).get("full_size")
            if full:
                entry = {"compressed_gib": full / 2**30, "uncompressed_gib": 2.6 * full / 2**30}
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                break
            time.sleep(2 + attempt * 5)
        except OSError:
            time.sleep(2 + attempt * 5)
    cache[image] = entry
    return entry


def cmd_select(args):
    """Tasks drawn uniformly at random within each image family, in proportion to the family's share of training
    rollouts; a task is kept with all its complete groups. Stop when the next image would exceed the disk budget.
    Image size never affects the order, so the sample is not biased toward small images."""
    rows = {f"{r['instance_id']}_{r['sample']}@{r['shard']}": r for r in load_index()}
    groups = json.loads(GROUPS.read_text())["groups"]
    size_path = DATA / "image-sizes.json"
    sizes = json.loads(size_path.read_text()) if size_path.exists() else {}
    full = [g for g in groups if len(g["members"]) == args.group_size
            and all(rows[m]["exit_status"] and rows[m]["commands"] > 0 for m in g["members"])]
    tasks = collections.defaultdict(list)
    for g in full:
        tasks[g["image"]].append(g)
    share = collections.Counter(rows[m]["family"] for g in full for m in g["members"])
    total = sum(share.values())
    rng = random.Random(args.seed)
    order = {fam: [t for t in sorted(tasks) if tasks[t][0]["family"] == fam] for fam in share}
    for fam in order:
        rng.shuffle(order[fam])
    chosen, used, skipped = [], 0.0, []
    # Interleave families so each keeps its rollout share while the budget lasts.
    taken = collections.Counter()
    while True:
        need = {fam: taken[fam] / max(1, len(chosen)) - share[fam] / total for fam in share if order[fam]}
        if not need:
            break
        fam = min(need, key=need.get)
        image = order[fam].pop(0)
        gib = hub_size(image, sizes)["uncompressed_gib"]
        if gib is None:
            skipped.append(image)
            continue
        if used + gib > args.budget_gib:
            break
        chosen.append(image)
        taken[fam] += 1
        used += gib
    size_path.write_text(json.dumps(sizes, indent=1))
    selected = [g for image in chosen for g in tasks[image]]
    SELECTION.write_text(json.dumps({"seed": args.seed, "budget_gib": args.budget_gib, "group_size": args.group_size,
                                     "family_share": dict(share), "images": chosen, "estimated_gib": round(used, 1),
                                     "unsized_skipped": skipped, "groups": selected}, indent=1))
    print(f"{len(chosen)} images ({dict(taken)}), {len(selected)} groups, {sum(len(g['members']) for g in selected)} "
          f"rollouts, ~{used:.0f} GiB; population share {dict(share)}")


def job_steps(rows, wave_gap_s=60, min_wave=32):
    """Training steps of each run rebuilt from rollout timing: a rollout was launched at first_ts - env_creation_s;
    a launch gap over `wave_gap_s` starts a new wave, waves of fewer than `min_wave` rollouts (relaunches) are
    dropped, and a wave launched before the previous wave's last rollout ended joins that wave's step.
    Returns {run: [step = [wave = rows sorted by launch]]}."""
    by = collections.defaultdict(list)
    for r in rows:
        if r["first_ts"] is not None:
            by[r["run"]].append(r)
    out = {}
    for run, rs in by.items():
        rs.sort(key=lambda r: r["first_ts"] - (r["env_creation_s"] or 0))
        waves = [[rs[0]]]
        for r in rs[1:]:
            last = waves[-1][-1]
            if (r["first_ts"] - (r["env_creation_s"] or 0)) - (last["first_ts"] - (last["env_creation_s"] or 0)) > wave_gap_s:
                waves.append([r])
            else:
                waves[-1].append(r)
        steps = []
        for w in (w for w in waves if len(w) >= min_wave):
            launch = w[0]["first_ts"] - (w[0]["env_creation_s"] or 0)
            if steps and launch < max(x["last_ts"] for x in steps[-1][-1]):
                steps[-1].append(w)
            else:
                steps.append([w])
        out[run] = steps
    return out


def cmd_plan(args):
    """Step plan for `sandbox.py pilot --sync-plan`: consecutive two-wave steps of one original run, each wave
    scaled down to `--groups-per-wave` complete groups drawn at random within image family (in the wave's family
    proportions), never reusing a group. Steps with a wave of fewer than `--min-own-groups` complete groups are
    skipped. Every member boots after its recorded delay from the wave's launch (launch spread + env creation).
    Wave offset and idle are fractions of `--step-estimate-s`, or the step's own recorded seconds with --timing orig.
    `--pool selection` draws from the built 46-image selection (candidates-rl.json, groups may repeat) for smoke
    tests; `--pool all` draws from every complete group and needs `convert --plan` and image builds."""
    rows = load_index()
    key = {f"{r['instance_id']}_{r['sample']}@{r['shard']}": r for r in rows}
    steps = job_steps(rows)
    runs = sorted(steps)
    run = next(r for r in runs if r.startswith(args.run)) if args.run else runs[-1]
    where = {}  # rollout -> (run, step, wave)
    for rn, run_steps in steps.items():
        for si, step in enumerate(run_steps):
            for wi, wave in enumerate(step):
                for r in wave:
                    where[f"{r['instance_id']}_{r['sample']}@{r['shard']}"] = (rn, si, wi)
    all_groups = json.loads(GROUPS.read_text())["groups"]
    if args.pool == "selection":
        candidates = json.loads(CANDIDATES.read_text())["traces"]
        by_name = collections.defaultdict(list)
        for e in candidates:
            if (HERE / ".local/images" / e["image_key"] / "image.json").exists():  # built images only
                by_name[e["group"]].append(e["name"])
        member_keys = {}
        for g in all_groups:
            names = sorted(m.split("@")[0] for m in g["members"])
            member_keys[tuple(names)] = g["members"]
        pool = {gid: member_keys[tuple(sorted(names))] for gid, names in by_name.items()
                if len(names) == args.group_size}
        candidates_file = str(CANDIDATES)
    else:
        pool = {f"G{i:04d}": g["members"] for i, g in enumerate(all_groups)
                if len(g["members"]) == args.group_size and g["run"] == run
                and all(key[m]["exit_status"] and key[m]["commands"] > 0 for m in g["members"])}
        candidates_file = str(args.candidates_out or HERE / "candidates-plan.json")
    # A group's place in the original job: the (step, wave) holding all its members.
    home = {gid: {where.get(m) for m in members} for gid, members in pool.items()}
    in_wave = collections.defaultdict(list)
    for gid, locs in home.items():
        if len(locs) == 1 and None not in locs:
            rn, si, wi = next(iter(locs))
            in_wave[(si, wi)].append(gid)
    rng = random.Random(args.seed)
    used = set()
    out_steps = []
    # Some second waves kept only a few members per task in the dataset (e.g. 91 rollouts of 25 tasks, no complete
    # group); topping such a wave up from another step would change the step's shape, so those steps are skipped.
    min_own = args.groups_per_wave // 2 if args.min_own_groups is None else args.min_own_groups
    two_wave = [si for si, step in enumerate(steps[run]) if len(step) == 2 and si >= args.start_step
                and (args.pool == "selection" or all(len(in_wave.get((si, wi), [])) >= min_own for wi in range(2)))]
    for si in two_wave[:args.steps]:
        step = steps[run][si]
        launches = [w[0]["first_ts"] - (w[0]["env_creation_s"] or 0) for w in step]
        end = max(x["last_ts"] for w in step for x in w)
        nxt = steps[run][si + 1] if si + 1 < len(steps[run]) else None
        orig = {"length_s": round(end - launches[0], 1), "wave_offset_s": round(launches[1] - launches[0], 1),
                "idle_after_s": round(nxt[0][0]["first_ts"] - (nxt[0][0]["env_creation_s"] or 0) - end, 1) if nxt else None,
                "rollouts": [len(w) for w in step]}
        waves = []
        for wi, wave in enumerate(step):
            share = collections.Counter(r["family"] for r in wave)
            # This wave's own groups first; a short wave is topped up from the same wave of the following steps.
            choices = []
            for sj in range(si, len(steps[run])):
                if args.pool == "selection":
                    # Smoke tests: any group of the built selection, unused ones first, from any wave.
                    options = [g for g in sorted(pool) if g not in used and g not in choices] or \
                              [g for g in sorted(pool) if g not in choices]
                else:
                    options = [g for g in in_wave.get((sj, wi), []) if g not in used and g not in choices]
                fam_options = collections.defaultdict(list)
                for g in options:
                    fam_options[key[pool[g][0]]["family"]].append(g)
                for fam in sorted(fam_options):
                    rng.shuffle(fam_options[fam])
                while len(choices) < args.groups_per_wave and any(fam_options.values()):
                    taken = collections.Counter(key[pool[g][0]]["family"] for g in choices)
                    fam = min((f for f in fam_options if fam_options[f]),
                              key=lambda f: (taken[f] / max(1, len(choices)) - share[f] / len(wave), f))
                    choices.append(fam_options[fam].pop())
                if len(choices) >= args.groups_per_wave:
                    break
            if len(choices) < args.groups_per_wave:
                raise SystemExit(f"step {si} wave {wi}: only {len(choices)} groups left")
            used.update(choices)
            delays = {}
            for g in choices:
                for m in pool[g]:
                    r = key[m]
                    # Recorded delay from this wave's launch for its own members; groups taken from other waves keep
                    # their delay from their own wave's launch.
                    loc = where.get(m)
                    base = steps[loc[0]][loc[1]][loc[2]][0] if loc else min(
                        (key[x] for x in pool[g]), key=lambda x: x["first_ts"] - (x["env_creation_s"] or 0))
                    base_launch = base["first_ts"] - (base["env_creation_s"] or 0)
                    launch = r["first_ts"] - (r["env_creation_s"] or 0)
                    delays[f"{r['instance_id']}_{r['sample']}"] = round(
                        (launch - base_launch) + args.env_scale * (r["env_creation_s"] or 0), 1)
                # Selection groups can hold a relaunched member (outside every wave, launched much later):
                # it gets the median delay of its group.
                lost = [m for m in pool[g] if m not in where]
                if lost and len(lost) < len(pool[g]):
                    kept = sorted(delays[m.split("@")[0]] for m in pool[g] if m in where)
                    for m in lost:
                        delays[m.split("@")[0]] = kept[len(kept) // 2]
            waves.append({"groups": choices,
                          "source_steps": sorted({where[pool[g][0]][1] for g in choices if where.get(pool[g][0])}),
                          "start_delay_s": delays})
        if args.timing == "orig":
            waves[1]["offset_s"] = orig["wave_offset_s"]
            idle = orig["idle_after_s"] or 0.0
        else:
            waves[1]["offset_s"] = round(args.wave_offset_frac * args.step_estimate_s, 1)
            idle = round(args.idle_frac * args.step_estimate_s, 1)
        waves[0]["offset_s"] = 0.0
        out_steps.append({"orig_step": si, "orig": orig, "waves": waves, "idle_after_s": idle,
                          "images": sorted({key[pool[g][0]]["image"] for w in waves for g in w["groups"]})})
    if len(out_steps) < args.steps:
        raise SystemExit(f"run {run} has only {len(out_steps)} two-wave steps from step {args.start_step}")
    plan = {"source_run": run, "seed": args.seed, "pool": args.pool, "candidates": candidates_file,
            "group_size": args.group_size, "groups_per_wave": args.groups_per_wave, "timing": args.timing,
            "scale": {"wave_offset_frac": args.wave_offset_frac, "idle_frac": args.idle_frac,
                      "step_estimate_s": args.step_estimate_s, "env_scale": args.env_scale},
            "steps": out_steps, "members": {g: [m.split("@")[0] for m in pool[g]]
                                            for s in out_steps for w in s["waves"] for g in w["groups"]}}
    args.out.write_text(json.dumps(plan, indent=1) + "\n")
    images = {i for s in out_steps for i in s["images"]}
    delays = sorted(d for s in out_steps for w in s["waves"] for d in w["start_delay_s"].values())
    print(f"{len(out_steps)} steps from {run} (orig steps {[s['orig_step'] for s in out_steps]}), "
          f"{len(images)} images, start delay p50/p90/max {delays[len(delays) // 2]}/{delays[int(len(delays) * .9)]}/"
          f"{delays[-1]} s -> {args.out}")


def convert_one(record, row):
    """Per-turn token counts are estimated from characters with the rollout's own ratio
    (all_tokens_length / characters of all messages); the dataset keeps token ids only for the whole rollout."""
    inst = record["instance"]
    messages = []
    ratio = (record.get("all_tokens_length") or 0) / max(1, row["total_chars"])
    chars_before = 0
    for m in record["messages"]:
        content = m.get("content") or ""
        if m["role"] == "assistant":
            actions = [{"command": a["command"]} for a in (m.get("extra") or {}).get("actions") or []]
            t = THINK.search(content)
            messages.append({"role": "assistant", "content": content,
                             "extra": {"actions": actions, "timestamp": m.get("timestamp"),
                                       "tokens": {"context": round(chars_before * ratio),
                                                  "output": round(len(content) * ratio),
                                                  "think": round((len(t.group(1)) if t else 0) * ratio)}}})
        elif m["role"] == "tool":
            rc = RETURNCODE.search(content)
            messages.append({"role": "tool", "content": content,
                             "extra": {"returncode": int(rc.group(1)) if rc else None, "timestamp": m.get("timestamp")}})
        else:
            messages.append({"role": m["role"], "content": content})
        chars_before += len(content)
    environment = {"image": inst["image_url"], "cwd": inst.get("workdir") or "/testbed",
                   "env": {"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R", "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1"},
                   "forward_env": [], "timeout": 60, "interpreter": ["bash", "-c"],
                   # pre_commands end with a literal backslash-n in the dataset; bash would read it as an "n" suffix
                   "setup": re.sub(r"(\\n)+$", "", (inst.get("pre_commands") or "").strip())}
    return {"instance_id": row["instance_id"], "trajectory_format": "rl-iter2->mini",
            "info": {"config": {"environment_type": "minisweagent.environments.docker.DockerEnvironment",
                                "environment": environment},
                     "exit_status": record.get("exit_status"), "submission": record.get("model_patch") or "",
                     "source": {"dataset": "sweagent/iter2-rl-rollouts", "member": row["member"], "shard": row["shard"],
                                "all_tokens_length": record.get("all_tokens_length"),
                                "response_tokens": len(record.get("response_token_ids") or [])}},
            "messages": messages}


def convert_shard(task):
    shard, wanted = task
    done = []
    with tarfile.open(RAW / shard, "r:gz") as tf:
        for member in tf:
            if member.name in wanted:
                row = wanted[member.name]
                record = json.load(tf.extractfile(member))
                path = TRACES / f"{row['instance_id']}_{row['sample']}.traj.json"
                path.write_text(json.dumps(convert_one(record, row)))
                done.append(member.name)
    return done


def cmd_convert(args):
    """Convert the selection (-> candidates-rl.json), or with --plan the plan's groups (-> the plan's candidates file,
    group ids as in the plan)."""
    rows = {f"{r['instance_id']}_{r['sample']}@{r['shard']}": r for r in load_index()}
    if args.plan:
        plan = json.loads(args.plan.read_text())
        if plan["pool"] != "all":
            raise SystemExit("--plan with pool selection uses candidates-rl.json as it is")
        groups = json.loads(GROUPS.read_text())["groups"]
        selection = {"group_size": plan["group_size"], "seed": plan["seed"],
                     "groups": [{**groups[int(gid[1:])], "id": gid} for gid in sorted(plan["members"])]}
        out = Path(plan["candidates"])
    else:
        selection = json.loads(SELECTION.read_text())
        out = CANDIDATES
    TRACES.mkdir(parents=True, exist_ok=True)
    by_shard = collections.defaultdict(dict)
    for g in selection["groups"]:
        for m in g["members"]:
            r = rows[m]
            by_shard[r["shard"]][r["member"]] = r
    todo = {shard: {m: r for m, r in wanted.items()
                    if not (TRACES / f"{r['instance_id']}_{r['sample']}.traj.json").exists()}
            for shard, wanted in by_shard.items()}
    todo = {shard: wanted for shard, wanted in todo.items() if wanted}
    converted = 0
    if todo:
        with ProcessPoolExecutor(len(todo)) as pool:
            converted = sum(len(x) for x in pool.map(convert_shard, todo.items()))
    entries = []
    for gi, g in enumerate(selection["groups"]):
        for m in g["members"]:
            r = rows[m]
            name = f"{r['instance_id']}_{r['sample']}"
            setup = ""
            path = TRACES / f"{name}.traj.json"
            env = json.loads(path.read_text())["info"]["config"]["environment"]
            setup = env.get("setup") or ""
            entries.append({"name": name, "trace": str(path), "image": r["image"],
                            "image_key": re.sub(r"[^A-Za-z0-9_.-]", "_", r["image"].removeprefix("docker.io/")),
                            "cwd": r["workdir"], "setup": setup, "family": r["family"],
                            "group": g.get("id", f"g{gi:03d}"), "run": g["run"], "commands": r["commands"],
                            "exit_status": r["exit_status"], "resolved": r["resolved"],
                            "recorded_window_s": round((r["last_ts"] or 0) - (r["first_ts"] or 0), 3),
                            "recorded_gap_s": r["llm_gap_sum_s"], "memory_mib": 2048})
    out.write_text(json.dumps({"criteria": {"source": "sweagent/iter2-rl-rollouts",
                                            "selection": str(args.plan or SELECTION),
                                            "group_size": selection["group_size"], "seed": selection["seed"]},
                               "traces": entries}, indent=1) + "\n")
    print(f"converted {converted} new rollouts -> {TRACES}; {len(entries)} entries -> {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("index").set_defaults(func=cmd_index)
    p = sub.add_parser("groups")
    p.add_argument("--gap", type=int, default=64, help="Max sample-index jump inside a group")
    p.add_argument("--window-s", type=float, default=3600, help="Max mtime distance from the group's first member")
    p.set_defaults(func=cmd_groups)
    p = sub.add_parser("select")
    p.add_argument("--seed", default="rl-select-1")
    p.add_argument("--group-size", type=int, default=8)
    p.add_argument("--budget-gib", type=float, default=150)
    p.set_defaults(func=cmd_select)
    p = sub.add_parser("plan")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", default="plan-1")
    p.add_argument("--run", help="Run name prefix (default: the later, 50 h run)")
    p.add_argument("--start-step", type=int, default=0, help="First original step (two-wave steps only are used)")
    p.add_argument("--steps", type=int, default=2)
    p.add_argument("--group-size", type=int, default=8)
    p.add_argument("--groups-per-wave", type=int, default=12)
    p.add_argument("--min-own-groups", type=int,
                   help="Skip steps with a wave of fewer complete groups of its own (default: groups-per-wave / 2)")
    p.add_argument("--pool", choices=("all", "selection"), default="all")
    p.add_argument("--candidates-out", type=Path,
                   help="Candidates file that `convert --plan` writes for this plan (default candidates-plan.json)")
    p.add_argument("--timing", choices=("scaled", "orig"), default="scaled")
    p.add_argument("--step-estimate-s", type=float, default=840, help="Expected step length for --timing scaled")
    p.add_argument("--wave-offset-frac", type=float, default=0.2)
    p.add_argument("--idle-frac", type=float, default=0.3)
    p.add_argument("--env-scale", type=float, default=1.0, help="Multiplier on recorded env creation time")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("convert")
    p.add_argument("--plan", type=Path, help="Convert this plan's groups instead of the selection")
    p.set_defaults(func=cmd_convert)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
