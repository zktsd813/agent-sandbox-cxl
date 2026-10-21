"""Draw the A-only migration breakdown from the final TAP results."""
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

base = Path(__file__).resolve().parents[2]
source = base / 'network-fairness-comparison.json'
out = Path(__file__).resolve().parent
data = json.loads(source.read_text())
primary = sorted(data['primary'], key=lambda row: row['memory_mib'])
assert [row['memory_mib'] for row in primary] == [512, 768, 1024, 1536, 2048]
assert all(row['measured_rounds'] == 20 for row in primary)
components = [
    ('pause_ms', 'Pause', '#94A3B8'),
    ('full_snapshot_ms', 'Snapshot', '#8B6FB5'),
    ('confirmed_transfer_ms', 'Transfer', '#4779C4'),
    ('remote_received_to_first_ack_ms', 'First response', '#E6A34D'),
    ('first_ack_to_working_set_ready_ms', 'Page scan', '#278C78'),
]
plt.rcParams.update({
    'font.family': 'DejaVu Sans', 'font.size': 12,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.edgecolor': '#b7c0cc', 'axes.labelcolor': '#263449',
    'xtick.color': '#43526a', 'ytick.color': '#43526a',
    'text.color': '#17263c', 'figure.facecolor': 'white',
    'axes.facecolor': 'white', 'savefig.facecolor': 'white',
    'pdf.fonttype': 42, 'svg.fonttype': 'none',
})
fig, ax = plt.subplots(figsize=(7.4, 4.7))
fig.subplots_adjust(left=.105, right=.99, top=.97, bottom=.235)
ax.set_axisbelow(True)
ax.grid(axis='y', color='#e7ebf0', linewidth=.8)
ax.tick_params(length=0, pad=6)
x = np.arange(len(primary))
bottom = np.zeros(len(primary))
handles = []
for metric, label, color in components:
    values = np.array([row['metrics'][metric]['mean'] / 1000 for row in primary])
    if metric == 'first_ack_to_working_set_ready_ms':
        bars = ax.bar(x, values, width=.46, bottom=bottom, facecolor='none',
                      edgecolor=color, linewidth=1.5, linestyle=(0, (3.2, 2.2)),
                      label=label, zorder=3)
    else:
        bars = ax.bar(x, values, width=.46, bottom=bottom, color=color,
                      label=label, edgecolor='none', zorder=3)
    handles.append(bars[0])
    bottom += values
assert np.allclose(bottom, [row['metrics']['pause_to_working_set_ready_ms']['mean'] / 1000 for row in primary], atol=1e-9)
page_scan = np.array([row['metrics']['first_ack_to_working_set_ready_ms']['mean'] / 1000
                      for row in primary])
without_page_scan = bottom - page_scan
for position, total, remaining in zip(x, bottom, without_page_scan):
    ax.annotate(f'{total:.2f}s ({remaining:.2f}s)', (position, total),
                xytext=(0, 7), textcoords='offset points', ha='center',
                va='bottom', fontsize=10.5, fontweight='medium', color='#263449')
ax.set_xticks(x, [str(row['memory_mib']) for row in primary], fontsize=11.5)
ax.set_yticks(np.arange(0, 11, 2))
ax.tick_params(axis='y', labelsize=11)
ax.set_xlabel('Guest memory (MiB)', fontsize=13, labelpad=9)
ax.set_ylabel('Time (s)', fontsize=13, labelpad=8)
ax.set_xlim(-.61, len(primary) - .39)
ax.set_ylim(0, 10.2)
fig.legend(handles, [label for _, label, _ in components], loc='lower center',
           bbox_to_anchor=(.535, .022), ncol=5, frameon=False, fontsize=9.2,
           columnspacing=.95, handlelength=1.2, handletextpad=.5)
for extension in ('png', 'pdf'):
    fig.savefig(out / ('migration-breakdown-tap.' + extension), dpi=180, bbox_inches='tight')
plt.close(fig)
rows = []
for row in primary:
    record = {'memory_mib': row['memory_mib'], 'n': row['measured_rounds']}
    for metric, _, _ in components:
        record[metric + '.mean'] = row['metrics'][metric]['mean']
    record['total_mean_ms'] = row['metrics']['pause_to_working_set_ready_ms']['mean']
    record['without_page_scan_mean_ms'] = (record['total_mean_ms']
        - row['metrics']['first_ack_to_working_set_ready_ms']['mean'])
    rows.append(record)
with (out / 'latency-data.csv').open('w', newline='') as file:
    writer = csv.DictWriter(file, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
(out / 'figure-source.json').write_text(json.dumps({
    'input': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'matplotlib': matplotlib.__version__,
    'notes': ['TAP/vhost migration profile, NIC IRQs on L1 CPU1.',
              'Source-clock means; five sequential intervals add to total.',
              'Compact layout; y axis fitted to TAP results (0..10.2s).',
              'Slim bars; labels are total (total minus plotted page-scan component), seconds, rounded to two decimals.',
              'Page scan shown as an unfilled dashed outline for on-demand access; no EPT-only interpretation.'],
}, indent=2) + '\n')
print(out / 'migration-breakdown-tap.png')
