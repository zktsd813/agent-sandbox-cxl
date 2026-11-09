#!/usr/bin/env python3
"""GSM8K parquet in verl's rule-reward format (same fields as verl examples/data_preprocess/gsm8k.py)."""
from pathlib import Path
import re
import sys

import pyarrow.parquet as pq
import pyarrow as pa

INSTRUCTION = 'Let\'s think step by step and output the final answer after "####".'


def convert(source, target, split):
    rows = []
    for index, row in enumerate(pq.read_table(source).to_pylist()):
        answer = re.search(r"#### (\-?[0-9\.\,]+)", row["answer"]).group(1).replace(",", "")
        rows.append({
            "data_source": "openai/gsm8k",
            "prompt": [{"role": "user", "content": f"{row['question']} {INSTRUCTION}"}],
            "ability": "math",
            "reward_model": {"style": "rule", "ground_truth": answer},
            "extra_info": {"split": split, "index": index},
        })
    pq.write_table(pa.Table.from_pylist(rows), target)
    print(target, len(rows))


if __name__ == "__main__":
    raw, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "test"):
        convert(next(raw.glob(f"main/{split}-*.parquet")), out / f"{split}.parquet", split)
