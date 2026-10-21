"""Compact comparison of the measured shared-DAX handoff paths."""
import csv
import hashlib
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator
import numpy as np

out = Path(__file__).resolve().parent
source = out.parents[1] / 'dax-size-comparison.json'
data = json.loads(source.read_text())
assert data['result'] == 'passed' and len(data['primary']) == 10
sizes = [512, 768, 1024, 1536, 2048]
models = [('reuse-prepared', 'State migration', '#8B6FB5', -.18),
          ('direct', 'Cross node context switch', '#4779C4', .18)]
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.edgecolor': '#b7c0cc', 'axes.labelcolor': '#263449',
    'xtick.color': '#43526a', 'ytick.color': '#43526a',
    'text.color': '#17263c', 'figure.facecolor': 'white',
    'savefig.facecolor': 'white', 'pdf.fonttype': 42, 'svg.fonttype': 'none'})

if '--with-full-migration' in sys.argv:
    fig, ax = plt.subplots(figsize=(7.4, 5.0))
    fig.subplots_adjust(left=.105, right=.99, top=.85, bottom=.265)
    ax.set_axisbelow(True)
    ax.grid(axis='y', color='#e7ebf0', linewidth=.8)
    ax.tick_params(length=0, pad=6)
    x = np.arange(len(sizes))
    width = .17
    green, orange = '#278C78', '#D99545'
    records = []
    for i, size in enumerate(sizes):
        row = next(r for r in data['full_ram_baseline']['primary'] if r['memory_mib'] == size)
        total = row['metrics']['pause_to_working_set_ready_ms']['mean'] / 1000
        ack = row['metrics']['pause_to_first_ack_ms']['mean'] / 1000
        assert total > 1 and 0 < ack <= total
        center = x[i] - .28
        left, right = center - width / 2, center + width / 2
        wave_x = np.linspace(left, right, 120)
        wave_y = .985 + .012 * np.sin(np.linspace(0, 2 * np.pi, 120))
        if ack < 1:
            ax.bar(center, ack, width=width, color=orange, edgecolor='none', zorder=3)
            ax.plot([left, left], [ack, .985], color=green, linewidth=1.35,
                    linestyle=(0, (3.2, 2.2)), zorder=3)
            ax.plot([right, right], [ack, .985], color=green, linewidth=1.35,
                    linestyle=(0, (3.2, 2.2)), zorder=3)
            ax.plot([left, right], [ack, ack], color=green, linewidth=1.35,
                    linestyle=(0, (3.2, 2.2)), zorder=3)
            wave_color = green
        else:
            # The ACK itself is above the cap: no page-scan portion is visible.
            ax.fill_between(wave_x, 0, wave_y, color=orange, linewidth=0, zorder=3)
            wave_color = '#B67A32'
        ax.plot(wave_x, wave_y, color=wave_color, linewidth=1.5, zorder=5)
        ax.plot(wave_x, wave_y - .03, color='white', linewidth=3.5, zorder=5)
        ax.plot(wave_x, wave_y - .03, color=wave_color, linewidth=1.2, zorder=6)
        ax.annotate(f'{total:.2f}s\n($\\mathbf{{{ack:.2f}s}}$)', (center, 1),
                    xytext=(0, 8), textcoords='offset points', ha='center', va='bottom',
                    fontsize=8, linespacing=1.15, color='#263449', annotation_clip=False)
        records.append({'model': 'full-migration', 'memory_mib': size,
                        'total_mean_ms': total * 1000, 'ack_mean_ms': ack * 1000,
                        'page_scan_mean_ms': (total - ack) * 1000, 'above_1s': True})
    for model, label, color, shift, label_y in [
            ('reuse-prepared', 'State migration', '#8B6FB5', 0, .085),
            ('direct', 'Cross node context switch', '#4779C4', .28, .22)]:
        for i, size in enumerate(sizes):
            row = next(r for r in data['primary'] if r['model'] == model and r['memory_mib'] == size)
            metrics = row['metrics_ms']
            ack = metrics['pause_to_first_ack_ms']['mean'] / 1000
            scan = metrics['first_ack_to_working_set_ready_ms']['mean'] / 1000
            total = metrics['total_pause_to_working_set_ready_ms']['mean'] / 1000
            assert np.isclose(ack + scan, total) and total < 1
            center = x[i] + shift
            ax.bar(center, ack, width=width, color=color, edgecolor='none', zorder=3)
            ax.bar(center, scan, bottom=ack, width=width, facecolor='none',
                   edgecolor=green, linewidth=1.25, linestyle=(0, (3.2, 2.2)), zorder=3)
            ax.annotate(f'{total * 1000:.2f}ms\n($\\mathbf{{{ack * 1000:.2f}ms}}$)',
                        xy=(center, total), xytext=(center, label_y),
                        ha='center', va='bottom', fontsize=7.5, linespacing=1.15,
                        color='#263449', arrowprops={'arrowstyle': '-', 'lw': .6, 'color': color},
                        zorder=5)
            records.append({'model': model, 'memory_mib': size,
                            'total_mean_ms': total * 1000, 'ack_mean_ms': ack * 1000,
                            'page_scan_mean_ms': scan * 1000, 'above_1s': False})
    ax.set_xticks(x, [str(size) for size in sizes], fontsize=11.5)
    ax.set_xlabel('Guest memory (MiB)', fontsize=13, labelpad=9)
    ax.set_ylabel('Time (s)', fontsize=13, labelpad=8)
    ax.set_xlim(-.61, len(sizes) - .39)
    ax.set_ylim(0, 1)
    ax.set_yticks(np.arange(0, 1.01, .2))
    handles = [Patch(facecolor=orange, edgecolor='none', label='Full migration'),
               Patch(facecolor='#8B6FB5', edgecolor='none', label='State migration'),
               Patch(facecolor='#4779C4', edgecolor='none', label='Cross node context switch'),
               Patch(facecolor='none', edgecolor=green, linewidth=1.35,
                     linestyle=(0, (3.2, 2.2)), label='Page scan')]
    fig.legend(handles=handles, loc='lower center', bbox_to_anchor=(.54, .012),
               ncol=2, frameon=False, fontsize=9.8, handlelength=1.2,
               columnspacing=1.7, labelspacing=.7)
    for extension in ('png', 'pdf'):
        fig.savefig(out / ('migration-three-way-1s.' + extension), dpi=180, bbox_inches='tight')
    plt.close(fig)
    with (out / 'three-way-plot-data.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    (out / 'three-way-figure-source.json').write_text(json.dumps({
        'input': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'matplotlib': matplotlib.__version__, 'statistics': 'Source-clock means, 20 measured per size and model.',
        'axis_limit_seconds': 1, 'overflow': 'Full migration exceeds 1s; double wavy bar end marks omitted height. Labels retain actual total (total minus page scan).',
        'small_bars': 'Actual heights, without a minimum display height; labels in ms with leaders.',
        'baseline': data['full_ram_baseline']['provenance'],
        'limits': data['limits'],
    }, indent=2) + '\n')
    print(out / 'migration-three-way-1s.png')
    raise SystemExit

fig, ax = plt.subplots(figsize=(7.4, 4.7))
fig.subplots_adjust(left=.105, right=.99, top=.97, bottom=.235)
ax.set_axisbelow(True)
ax.grid(axis='y', color='#e7ebf0', linewidth=.8)
ax.tick_params(length=0, pad=6)
x = np.arange(len(sizes))
records = []
peak = 0
for model, label, color, shift in models:
    rows = [next(r for r in data['primary'] if r['model'] == model and r['memory_mib'] == size)
            for size in sizes]
    ack = np.array([r['metrics_ms']['pause_to_first_ack_ms']['mean'] for r in rows])
    scan = np.array([r['metrics_ms']['first_ack_to_working_set_ready_ms']['mean'] for r in rows])
    total = np.array([r['metrics_ms']['total_pause_to_working_set_ready_ms']['mean'] for r in rows])
    assert np.allclose(ack + scan, total, atol=1e-9)
    ax.bar(x + shift, ack, width=.26, color=color, edgecolor='none', zorder=3)
    ax.bar(x + shift, scan, bottom=ack, width=.26, facecolor='none',
           edgecolor='#278C78', linewidth=1.35, linestyle=(0, (3.2, 2.2)), zorder=3)
    for position, height, remaining in zip(x + shift, total, ack):
        ax.annotate(f'{height:.2f}ms\n({remaining:.2f}ms)', (position, height),
                    xytext=(0, 6), textcoords='offset points', ha='center',
                    va='bottom', fontsize=8, linespacing=1.15, color='#263449')
    peak = max(peak, float(total.max()))
    for row in rows:
        metrics = row['metrics_ms']
        records.append({'model': model, 'memory_mib': row['memory_mib'],
            'working_set_mib': row['working_set']['bytes'] / 2**20,
            'ack_mean_ms': metrics['pause_to_first_ack_ms']['mean'],
            'ack_p50_ms': metrics['pause_to_first_ack_ms']['p50'],
            'ack_p95_ms': metrics['pause_to_first_ack_ms']['p95'],
            'total_mean_ms': metrics['total_pause_to_working_set_ready_ms']['mean'],
            'page_scan_mean_ms': metrics['first_ack_to_working_set_ready_ms']['mean']})
ax.set_xticks(x, [str(size) for size in sizes], fontsize=11.5)
ax.set_xlabel('Guest memory (MiB)', fontsize=13, labelpad=9)
ax.set_ylabel('Time (ms)', fontsize=13, labelpad=8)
ax.set_xlim(-.61, len(sizes) - .39)
ax.set_ylim(0, peak * 1.18 + 1)
ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
handles = [Patch(facecolor=color, edgecolor='none', label=label) for _, label, color, _ in models]
handles.append(Patch(facecolor='none', edgecolor='#278C78', linewidth=1.35,
                     linestyle=(0, (3.2, 2.2)), label='Page scan'))
fig.legend(handles=handles, loc='lower center', bbox_to_anchor=(.54, .025),
           ncol=3, frameon=False, fontsize=10, handlelength=1.2, columnspacing=1.5)
for extension in ('png', 'pdf'):
    fig.savefig(out / ('dax-handoff-comparison.' + extension), dpi=180, bbox_inches='tight')
plt.close(fig)
with (out / 'plot-data.csv').open('w', newline='') as file:
    writer = csv.DictWriter(file, fieldnames=list(records[0]))
    writer.writeheader()
    writer.writerows(records)
(out / 'figure-source.json').write_text(json.dumps({
    'input': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'matplotlib': matplotlib.__version__, 'statistics': 'Means of20 measured handoffs; four warmups excluded.',
    'labels': 'Total pause-to-W-ready (total minus plotted page scan), milliseconds.',
    'page_scan': 'Source ACK-to-W-ready observation; includes workload reads and demand/cold effects, not isolated EPT time.',
}, indent=2) + '\n')
print(out / 'dax-handoff-comparison.png')
