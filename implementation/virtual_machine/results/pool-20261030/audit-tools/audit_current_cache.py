import argparse, hashlib, json, pathlib, re

parser=argparse.ArgumentParser(description='Independent exact current guest-cache correspondence audit.')
parser.add_argument('report',type=pathlib.Path)
parser.add_argument('output',type=pathlib.Path)
args=parser.parse_args()
RUN=args.report.resolve().parent
d=json.loads((RUN/'report.json').read_text())
receipts={r['file']:r for r in d['receipts']}
used=set()
def blob(name):
    data=(RUN/name).read_bytes()
    assert hashlib.sha256(data).hexdigest()==receipts[name]['sha256'],name
    used.add(name)
    return data
def text(name):
    return blob(name).decode().replace('\r','')
def jsonline(name,prefix):
    lines=[json.loads(x[len(prefix):]) for x in text(name).splitlines() if x.startswith(prefix)]
    assert len(lines)==1
    return lines[0]
audits={}
def collect(value):
    if isinstance(value,dict):
        if value.get('current_file_cache_proven'):
            audits[value['file']]=value
        else:
            for v in value.values():collect(v)
    elif isinstance(value,list):
        for v in value:collect(v)
collect(d)
results=[]
by_file={}
for name,a in sorted(audits.items()):
    probe=a['cache_probe']['receipt']
    assert probe['op']=='cachemap' and probe['block_read_ios']==0
    assert probe['resident_before']*4096==d['payload_bytes']
    actualprobe=[json.loads(x) for x in text(a['cache_probe']['file']).splitlines() if x.startswith('{"event":"pool_guest"')]
    assert actualprobe==[probe]
    entries=[]
    range_lines=text(a['cache_gpa_file']).splitlines()
    for line in range_lines:
        parts=line.split()
        assert len(parts)==5 and parts[:2]==['POOL_CACHE_RANGE',probe['label']]
        offset,gpa,count=map(int,parts[2:])
        assert offset%4096==gpa%4096==0 and gpa>0 and count>0
        entries.extend((offset+i*4096,gpa+i*4096) for i in range(count))
    assert [x[0] for x in entries]==list(range(0,d['payload_bytes'],4096))
    assert len({x[1] for x in entries})==len(entries)==a['cache_pages']
    admission=json.loads(blob(a['admission_file']))
    assert admission['vm_id']==str(a['vm']) and admission['run_epoch']==str(a['epoch'])
    mapped={p['gpa']:p for p in admission['mapped_pages']}
    selected=[]
    pool_to_file={}
    for off,gpa in entries:
        rem=off
        for disk_start,length in d['fixture_manifest']['pool']['image']['payload_extents']:
            if rem<length:
                image_off=disk_start+rem
                break
            rem-=length
        else: raise AssertionError(('extent',off))
        row=mapped.get(gpa)
        if row and row['pool_offset']==d['pool_offset']+image_off:
            selected.append(row)
            pool_to_file[row['pool_offset']]=off
    assert len(entries)-len(selected)==a['unmatched_cache_pages']
    expected=''.join(f"{r['hva']} {r['pool_offset']}\n" for r in selected).encode()
    actual=blob(a['pfn_input_file'])
    assert actual==expected and hashlib.sha256(actual).hexdigest()==a['pfn_input_sha256']
    offsets=list(map(int,blob(a['shared_offsets_file']).splitlines()))
    pfn=jsonline(name,'POOL_PFN_JSON ')
    assert pfn['rows']==len(selected) and pfn['shared_pages']==len(offsets)
    assert pfn['all_rows_checked'] and pfn['shared_bytes']==len(offsets)*4096
    assert set(offsets)<=set(pool_to_file) and sorted(set(offsets))==a['shared_offsets']
    files=sorted(pool_to_file[x] for x in set(offsets))
    assert files==a['shared_file_offsets']
    assert len(files)*4096>=128<<20
    by_file[name]=set(files)
    results.append({'file':name,'vm':a['vm'],'epoch':a['epoch'],'role':a['role'],
                    'raw_range_lines':len(range_lines),'current_cache_pages':len(entries),
                    'matched_pages':len(selected),'unmatched_pages':a['unmatched_cache_pages'],
                    'current_shared_file_pages':len(files),'current_shared_file_bytes':len(files)*4096})
groups=[]
for group in d.get('concurrent_sharing',[]):
    aa=group['audits']
    names=[a['file'] for a in aa]
    frozen={a['frozen_group_file'] for a in aa}
    assert len(frozen)==1 and len(aa)==3 and {a['vm'] for a in aa}=={1,2,3}
    freeze=next(iter(frozen))
    assert '\nPOOL_CACHE_GROUP_FROZEN 1 2 3\n' in text(freeze)
    for a in aa:
        assert a['cache_probe']['file']<freeze<a['cache_gpa_file']<a['pfn_input_file']<a['file']
        paused=[name for name,r in receipts.items() if r['kind']=='api' and
                a['cache_probe']['file']<name<freeze and
                f"/vm{a['vm']}-epoch{a['epoch']}/" in text(name).replace('\n','') and 'Paused' in text(name)]
        assert len(paused)==1, (freeze,a['vm'],paused)
        assert jsonline(paused[0], 'LAB_API_JSON ')['status'] == 204
    for name,r in receipts.items():
        if r['kind']=='api' and freeze<name<=max(names):
            assert 'Resumed' not in text(name), (freeze,name)
    common=set.intersection(*(by_file[name] for name in names))
    assert len(common)==group['shared_current_file_pages_all_vms']
    assert len(common)*4096==group['shared_current_file_bytes_all_vms']>=128<<20
    groups.append({'cycle':group['cycle'],'stage':group['stage'],'frozen_group_file':freeze,
                   'vm_roles':group['roles'],'common_current_file_pages':len(common),
                   'common_current_file_bytes':len(common)*4096,'all_three_paused_during_all_scans':True})
out={'run_result_at_read':d['result'],'completed_transitions_at_read':len(d['transitions']),
     'method':'Independent raw guest cache-range expansion and exact file-offset/GPA/admission-HVA/PFN-input join; no project verifier import.',
     'result':'passed' if d['result']=='passed' else 'passed_for_available_measurements',
     'report_sha256':hashlib.sha256((RUN/'report.json').read_bytes()).hexdigest(),'current_cache_audits':results,
     'frozen_three_vm_groups':groups,'used_raw_receipts_hashed':len(used)}
path=args.output
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out,indent=2))
