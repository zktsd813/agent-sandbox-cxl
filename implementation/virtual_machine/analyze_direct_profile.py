#!/usr/bin/env python3
"""Attribute DirectExport + InstanceStart using receipt-bound, opt-in VMM timings.

Top-level components are exclusive and add to the client API interval for EACH
handoff. Nested disk/VM wrapper timings are diagnostic subsets, never added again.
Means are additive; independently computed medians generally are not.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics


def need(condition, message):
    if not condition:
        raise ValueError(message)


def stats(values):
    values = sorted(values)
    return dict(n=len(values), mean=statistics.mean(values), p50=statistics.median(values),
                p95=values[math.ceil(len(values)*.95)-1], min=values[0], max=values[-1])


def analyze(path):
    report = json.loads(path.read_text())
    need(report['result'] == 'passed' and report['mode'] == 'direct', 'Passed direct report required')
    receipts = {r['file']: r for r in report['receipts']}

    def read(name):
        raw = (path.parent / name).read_bytes()
        need(hashlib.sha256(raw).hexdigest() == receipts[name]['sha256'], 'Receipt hash: ' + name)
        return raw.decode()

    logs, api_logs = {}, {}
    for launch in report['launches']:
        matches = [r for r in receipts.values() if r['kind'] == 'final-vmm-log' and
                   r['integrity']['path'] == launch['directory'] + '/vmm.log']
        need(len(matches) == 1, 'Missing/ambiguous final VMM log')
        text = read(matches[0]['file'])
        logs[launch['epoch']] = [json.loads(line.split('RESTORE_TIMING ', 1)[1])
                                for line in text.splitlines() if 'RESTORE_TIMING ' in line]
        api_logs[launch['epoch']] = [json.loads(line.split('DIRECT_API_TIMING ', 1)[1])
                                    for line in text.splitlines() if 'DIRECT_API_TIMING ' in line]

    def timing(epoch, scope):
        matches = [row for row in logs[epoch] if row['scope'] == scope]
        need(len(matches) == 1, f'Missing/ambiguous timing: {epoch} {scope}')
        row = matches[0]
        need(all(math.isfinite(v) and v >= 0 for v in row['stages_us'].values()), 'Invalid stage')
        need(row['total_us'] >= sum(row['stages_us'].values()) - .01, 'Timing interval overlap')
        return row

    rows = []
    for hop in report['transitions']:
        if hop['kind'] != 'direct':
            continue
        source, target = hop['source_epoch'], hop['destination_epoch']
        export, attach = timing(source, 'direct_export'), timing(target, 'direct_import')
        components = {f'export.{k}': v for k, v in export['stages_us'].items()}
        components.update({f'import.{k}': v for k, v in attach['stages_us'].items()})
        detach_us = hop['token']['kernel_ns'] / 1000
        attach_us = hop['import_event']['token']['kernel_ns'] / 1000
        components['export.export_detach_ioctls'] -= detach_us
        components['import.attach_and_vm_wrapper'] -= attach_us
        components['kernel.detach'] = detach_us
        components['kernel.attach'] = attach_us
        export_api = hop['export_api']['response']['duration_us']
        attach_api = hop['attach_api']['response']['duration_us']
        api_us = export_api + attach_api
        components['api_dispatch_response_and_profile_log'] = api_us - sum(components.values())
        need(all(v >= -.01 for v in components.values()), 'Nested interval exceeds parent')
        need(abs(api_us - sum(components.values())) < .01, 'Non-additive breakdown')
        # Confirm that the client timings were copied faithfully from raw receipts.
        for api in (hop['export_api'], hop['attach_api']):
            need(str(api['response']['duration_us']) in read(api['file']), 'API receipt disagreement')
        rows.append(dict(source=hop['source'], destination=hop['destination'], generation=hop['token']['generation'],
                         source_epoch=source, api_us=api_us, export_api_us=export_api, attach_api_us=attach_api,
                         kernel_us=detach_us+attach_us, nonkernel_us=api_us-detach_us-attach_us,
                         components_us=components, export=export, attach=attach,
                         disk=timing(target, 'disk_properties'), wrapper=timing(target, 'vm_local_wrapper')))
        if 'kernel_profile' in hop:
            kp = hop['kernel_profile']
            words = [int(s, 16) for s in re.findall(r'^LAB_DAX_WORD=0x([0-9a-f]{8})\s*$', read(kp['file']), re.M)]
            need(len(words) == 32, 'Kernel profile word count')
            ns = [words[i] | words[i+1] << 32 for i in range(0, 32, 2)]
            need(ns == kp['detach_ns'] + kp['attach_ns'], 'Kernel profile differs from receipt')
            need(sum(ns[:8]) > 0 and sum(ns[8:]) > 0, 'Kernel profile disabled')
            rows[-1]['kernel_substages_us'] = {
                **{f'detach.{i}': v/1000 for i, v in enumerate(ns[:8])},
                **{f'attach.{i}': v/1000 for i, v in enumerate(ns[8:])}}
        if api_logs[source] and api_logs[target]:
            api_rows = []
            for epoch, event, main, client in ((source, 'export', export, export_api), (target, 'import', attach, attach_api)):
                found = [r for r in api_logs[epoch] if r['event'] == event]
                need(len(found) == 1, 'API profile missing/ambiguous')
                a = found[0]
                need(a['total_us'] == a['parse_us'] + a['vmm_roundtrip_us'] + a['response_us'], 'API profile overlap')
                api_rows.append(dict(event=event, **{k: a[k] for k in ('parse_us','vmm_roundtrip_us','response_us','total_us')},
                                     dispatch_and_main_profile_log_us=a['vmm_roundtrip_us']-main['total_us'],
                                     transport_and_api_profile_log_us=client-a['total_us']))
            rows[-1]['api_substages'] = api_rows
    need(rows, 'No direct handoffs')

    def summarize(group):
        result = dict(api_us=stats([r['api_us'] for r in group]),
                    kernel_us=stats([r['kernel_us'] for r in group]),
                    nonkernel_us=stats([r['nonkernel_us'] for r in group]),
                    components_us={k: stats([r['components_us'][k] for r in group])
                                   for k in group[0]['components_us']},
                    disk_substages_us={k: stats([r['disk']['stages_us'][k] for r in group])
                                       for k in group[0]['disk']['stages_us']})
        if all('kernel_substages_us' in r for r in group):
            result['kernel_substages_us'] = {k: stats([r['kernel_substages_us'][k] for r in group])
                                            for k in group[0]['kernel_substages_us']}
        if all('api_substages' in r for r in group):
            result['api_substages_us'] = {k: stats([sum(a[k] for a in r['api_substages']) for r in group])
                                         for k in ('parse_us', 'response_us', 'dispatch_and_main_profile_log_us',
                                                   'transport_and_api_profile_log_us')}
        return result
    return dict(schema='direct-api-profile-v1', report=str(path),
                report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                executable_sha256=report['fixture']['inputs']['firecracker']['sha256'],
                rows=rows, all=summarize(rows),
                by_destination={dest: summarize([r for r in rows if r['destination'] == dest]) for dest in ('A', 'B')},
                notes=['API = DirectExport + InstanceStart; pause, resume, source fence, process launch, metadata transport and guest ACK excluded',
                       'Clock sampling enabled; residual includes final profile logging and client/API dispatch/response scheduling',
                       'Nested subtimings overlap their parents; do not add them to exclusive components',
                       'Additive means; medians of individual components do not necessarily add to median API',
                       'No warmup samples discarded; first attach and post-workload return are retained',
                       'Independent workload verification must be run separately'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.report.resolve())
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result['all'][k] for k in ('api_us', 'kernel_us', 'nonkernel_us')}, indent=2))


if __name__ == '__main__':
    main()
