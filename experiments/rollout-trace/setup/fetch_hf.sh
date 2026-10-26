#!/usr/bin/env bash
# Download a public HF model or dataset into $DEST/<org>--<name> at a capped
# rate so the shared server's network stays usable. Resumes partial files,
# retries dropped connections and checks every file's size.
# usage: [DATA_ROOT=/data] fetch_hf.sh model|dataset <org/name> [rate, default 20M]
set -euo pipefail
kind=$1 repo=$2 rate=${3:-20M}
DATA_ROOT=${DATA_ROOT:-/mnt/ssd1/ijkim}
case $kind in
  model)   api=models;   url=https://huggingface.co/$repo;          base=$DATA_ROOT/models ;;
  dataset) api=datasets; url=https://huggingface.co/datasets/$repo; base=$DATA_ROOT/datasets ;;
esac
dest=${DEST:-$base}/${repo/\//--}
mkdir -p "$dest"

info=$(curl -fsS --retry 5 "https://huggingface.co/api/$api/$repo/revision/main?blobs=true")
rev=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha"])' "$info")
echo "$repo $rev" > "$dest/REVISION"

python3 -c 'import json,sys; [print(s["size"], s["rfilename"]) for s in json.loads(sys.argv[1])["siblings"]]' "$info" |
while read -r size f; do
  mkdir -p "$dest/$(dirname "$f")"
  for try in $(seq 1 30); do
    have=$(stat -c %s "$dest/$f" 2>/dev/null || echo 0)
    [ "$have" -eq "$size" ] && break
    [ "$have" -gt "$size" ] && rm -f "$dest/$f"
    curl -sSL -C - --limit-rate "$rate" -o "$dest/$f" "$url/resolve/$rev/$f" < /dev/null \
      || { echo "retry $try $f"; sleep 10; }
  done
  have=$(stat -c %s "$dest/$f" 2>/dev/null || echo 0)
  [ "$have" -eq "$size" ] || { echo "size mismatch $f: $have != $size"; exit 1; }
done
echo "done $repo $rev"
