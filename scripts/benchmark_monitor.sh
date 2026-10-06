#!/bin/bash
# Sample GPU, container and host resources until the process is stopped.
set -u
out="${1:-$HOME/benchmark-results/monitor}"
mkdir -p "$out"
nvidia-smi dmon -s pucm -o DT -d 5 > "$out/nvidia-dmon.log" &
echo $! > "$out/nvidia-dmon.pid"
echo "timestamp,name,cpu,mem_usage,net,block" > "$out/docker-stats.csv"
echo "timestamp,free_ram_mb,used_ram_mb" > "$out/memory.csv"
echo "timestamp,device,read_bytes,write_bytes,interval_ms" > "$out/disk.csv"
prev_reads=""
prev_writes=""
prev_ms=""
trap 'kill "$(cat "$out/nvidia-dmon.pid")" 2>/dev/null || true; exit' INT TERM EXIT
while true; do
  ts=$(date -Is)
  docker stats --no-stream --format "{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.NetIO}},{{.BlockIO}}" \
    | awk -v ts="$ts" '{print ts","$0}' >> "$out/docker-stats.csv" || true
  mem=$(awk '/MemAvailable|MemTotal/ {gsub(/kB/,""); print $2}' /proc/meminfo)
  total=$(echo "$mem" | sed -n 1p)
  avail=$(echo "$mem" | sed -n 2p)
  echo "$ts,$((avail/1024)),$(((total-avail)/1024))" >> "$out/memory.csv"
  now_ms=$(date +%s%3N)
  read -r reads writes < <(awk '$3 == "vda" {r=$6; w=$10} END {print r+0, w+0}' /proc/diskstats)
  if [ -n "$prev_reads" ]; then
    echo "$ts,vda,$(((reads-prev_reads)*512)),$(((writes-prev_writes)*512)),$((now_ms-prev_ms))" >> "$out/disk.csv"
  fi
  prev_reads=$reads
  prev_writes=$writes
  prev_ms=$now_ms
  gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null || echo "n/a")
  echo "$ts gpu=$gpu ram_used_mb=$(((total-avail)/1024))"
  sleep 5
done
