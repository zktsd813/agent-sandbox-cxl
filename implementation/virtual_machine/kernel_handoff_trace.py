"""Opt-in constant-size function probes, exclusively inside disposable L1s."""
import re
import shlex

FUNCTIONS={'arch':('kvm','kvm_arch_dax_handoff'),
           'ept':('kvm','kvm_mmu_dax_ept_rebind'),
           'vmx':('kvm_intel','vmx_dax_handoff'),
           'pit':('kvm','kvm_pit_dax_handoff'),
           'lapic':('kvm','kvm_lapic_dax_handoff')}
OPTIONAL_FUNCTIONS={'nested_root':('kvm','kvm_dax_nested_root_sync'),
                    'tlb_current':('kvm_intel','vmx_flush_tlb_current'),
                    'tlb_all':('kvm_intel','vmx_flush_tlb_all'),
                    'tlb_guest':('kvm_intel','vmx_flush_tlb_guest')}


def trajectory_start(c):
    # Histograms count in-kernel without filling a ring buffer per fault.
    # Ring tracing is reserved for the infrequent nested handoff probes.
    start(c)
    for role in ('A', 'B'):
        script = 'T=/sys/kernel/tracing\n'
        for name in ('tlb_current', 'tlb_all', 'tlb_guest'):
            for suffix in ('enter', 'exit'):
                script += f'if [ -d $T/events/resident_diag/{name}_{suffix} ]; then echo 0 > $T/events/resident_diag/{name}_{suffix}/enable; fi\n'
        for name, symbol in (('create_vm', 'kvm_arch_init_vm'), ('create_vcpu', 'kvm_arch_vcpu_create')):
            script += f"echo 'p:trajectory/{name} kvm:{symbol}' >> $T/kprobe_events\n"
        for event in ('kvm/kvm_exit', 'kvm/kvm_page_fault', 'trajectory/create_vm', 'trajectory/create_vcpu'):
            suffix = ' if exit_reason == 48' if event == 'kvm/kvm_exit' else ''
            script += f"echo 'hist:keys=common_pid{suffix}' > $T/events/{event}/trigger\n"
        c.shell(role, script, 'trajectory-count-start')


def trajectory_counts(c):
    result = {}
    for role in ('A', 'B'):
        counts = {}
        for name, event in (('ept_violation', 'kvm/kvm_exit'), ('page_fault', 'kvm/kvm_page_fault'),
                            ('create_vm', 'trajectory/create_vm'), ('create_vcpu', 'trajectory/create_vcpu')):
            raw, file = c.shell(role, f'cat /sys/kernel/tracing/events/{event}/hist', 'trajectory-'+name)
            totals = re.findall(r'Totals:\s*Hits:\s*(\d+)\s+Entries:\s*(\d+)\s+Dropped:\s*(\d+)', raw)
            if len(totals) != 1 or int(totals[0][2]):
                raise ValueError('Missing or dropped histogram: '+raw)
            counts[name] = int(totals[0][0])
            counts[name+'_file'] = file
        result[role] = counts
    return result


def trajectory_mappings(c, s):
    # Only this sandbox plus an empty keepalive VM remains in each L1. The
    # aggregate counters therefore measure its live leaves, including direct
    # imports whose per-VM debugfs directory is deliberately removed on detach.
    raw, file = c.shell(s.role, 'mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true\n'
        'for n in pages_4k pages_2m pages_1g; do\n'
        'printf "%s " "$n"; cat /sys/kernel/debug/kvm/$n\ndone', 'trajectory-spte')
    sizes = {name:int(value) for name,value in re.findall(r'^(pages_[421][kmg]) (\d+)\s*$', raw, re.M)}
    if set(sizes) != {'pages_4k', 'pages_2m', 'pages_1g'}:
        raise ValueError('Actual leaf mapping counters missing: '+raw)
    inactive = 'B' if s.role == 'A' else 'A'
    idle, idle_file = c.shell(inactive,
        'mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true\n'
        'for n in pages_4k pages_2m pages_1g; do\n'
        'printf "%s " "$n"; cat /sys/kernel/debug/kvm/$n\ndone', 'trajectory-spte-inactive')
    idle_sizes = {name:int(value) for name,value in re.findall(r'^(pages_[421][kmg]) (\d+)\s*$', idle, re.M)}
    if set(idle_sizes) != set(sizes) or any(idle_sizes.values()):
        raise ValueError('Inactive L1 contains unexpected mapped KVM RAM: '+idle)
    return {'role': s.role, 'file': file, **sizes, 'inactive': {'role': inactive, 'file': idle_file, **idle_sizes}}


def start(c, *, reuse=False):
    script='mount -t tracefs tracefs /sys/kernel/tracing 2>/dev/null || true\nT=/sys/kernel/tracing\necho 0 > $T/tracing_on\necho nop > $T/current_tracer\necho mono > $T/trace_clock\necho 1024 > $T/buffer_size_kb\necho > $T/trace\n'
    for name,(module,function) in ({} if reuse else FUNCTIONS | OPTIONAL_FUNCTIONS).items():
        if name in OPTIONAL_FUNCTIONS:
            # GCC may specialize this noinline helper as .isra.0. Resolve the
            # actual text symbol; a word grep also matches the unsuffixed prefix.
            query=f'$2 ~ /^[tT]$/ && ($3 == "{function}" || index($3, "{function}.") == 1) {{ print $3; exit }}'
            script+=f'symbol=$(awk {shlex.quote(query)} /proc/kallsyms)\nif [ -n "$symbol" ]; then\n'
            script+=f'echo TRACE_SYMBOL={name}:"$symbol"\n'
            script+=f'printf "p:resident_diag/{name}_enter {module}:%s\\n" "$symbol" >> $T/kprobe_events\n'
            script+=f'printf "r128:resident_diag/{name}_exit {module}:%s\\n" "$symbol" >> $T/kprobe_events\nfi\n'
        else:
            for definition in (f'p:resident_diag/{name}_enter {module}:{function} attach=$arg2:x32',
                               f'r128:resident_diag/{name}_exit {module}:{function}'):
                script+=f"printf '%s\\n' {shlex.quote(definition)} >> $T/kprobe_events\n"
    script+='echo 1 > $T/events/resident_diag/enable\necho 1 > $T/tracing_on'
    for role in ('A','B'):c.shell(role,script,'kernel-trace-start')


def mark(c,epoch):
    for role in ('A','B'):
        c.shell(role,f"echo RESIDENT_KERNEL_EPOCH={epoch} > /sys/kernel/tracing/trace_marker",'kernel-trace-marker')


def finish(c):
    rows=[]
    for role in ('A','B'):
        raw,name=c.shell(role,'T=/sys/kernel/tracing\necho 0 > $T/tracing_on\necho 0 > $T/events/resident_diag/enable\ncat $T/trace\ncat $T/per_cpu/cpu*/stats','kernel-trace')
        rows.append({'role':role,'file':name,'intervals':parse(raw)})
    return rows


def parse(text):
    if any(int(value)>0 for value in re.findall(r'^overrun:\s*(\d+)',text,re.MULTILINE)):
        raise ValueError('Kernel trace overflow')
    pattern=re.compile(r'^.*?-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+)\.(\d+):\s+(\w+):\s*(.*)$')
    epoch=None;stacks={};rows=[]
    for line in text.splitlines():
        match=pattern.match(line.strip())
        if not match:continue
        pid,cpu,sec,frac,event,fields=match.groups()
        timestamp=int(sec)*1000000000+int((frac+'000000000')[:9])
        marker=re.search(r'RESIDENT_KERNEL_EPOCH=(\d+)',fields)
        if marker:epoch=int(marker[1]);continue
        for kind in FUNCTIONS | OPTIONAL_FUNCTIONS:
            if event==kind+'_enter':
                arg=re.search(r'attach=(0x[0-9a-f]+|\d+)',fields)
                stacks.setdefault((kind,pid),[]).append((timestamp,epoch,bool(int(arg[1],0)) if arg else None))
            elif event==kind+'_exit':
                stack=stacks.get((kind,pid),[])
                if not stack:raise ValueError('Unmatched kernel trace return')
                begin,generation,attach=stack.pop()
                rows.append({'epoch':generation,'function':kind,'attach':attach,'duration_ns':timestamp-begin})
    if any(stacks.values()):raise ValueError('Incomplete kernel trace')
    if not rows:raise ValueError('No kernel trace intervals')
    return rows
