#!/usr/bin/env bash
# Continuous sampler for the Dispatcharr buffering investigation.
# Run this for the whole duration of each A/B test while actually watching TV,
# so every test produces numbers instead of impressions.
#
# Usage: ./dispatcharr-watch.sh [seconds] [interval]
#   ./dispatcharr-watch.sh 600        # 10 minutes, 5s samples
#   ./dispatcharr-watch.sh 1200 10 | tee test4-celery-off.log

set -uo pipefail

NS=dispatcharr
DURATION="${1:-600}"
INTERVAL="${2:-5}"

WEB_POD=$(kubectl -n $NS get pod -l app=dispatcharr,component=web \
  -o jsonpath='{.items[0].metadata.name}')
[[ -z "$WEB_POD" ]] && { echo "FATAL: no web pod"; exit 1; }

# Baseline counters so we report deltas, not absolutes.
read_throttle() {
  kubectl -n $NS exec "$WEB_POD" -c dispatcharr -- \
    awk '/nr_throttled/{t=$2} /throttled_usec/{u=$2} END{print t" "u}' /sys/fs/cgroup/cpu.stat 2>/dev/null \
    || echo "0 0"
}
read -r T0 U0 <<<"$(read_throttle)"
R0=$(kubectl -n $NS get pod -l app=dispatcharr,component=redis \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)

echo "# dispatcharr-watch  pod=$WEB_POD  duration=${DURATION}s  interval=${INTERVAL}s  start=$(date -Is)"
echo "# buf_speed <1.0 means the stream is falling behind real time."
echo "# thr_delta = new CPU-throttle events since start (any growth => CPU limit is biting)."
printf '%-9s %-8s %-11s %-11s %-9s %-9s %-8s %s\n' \
  TIME CLIENTS BUF_MIN BUF_AVG THR_DLT THRUSEC REDIS_RS RATES_MBPS
echo "--------------------------------------------------------------------------------------"

END=$(( $(date +%s) + DURATION ))
while [[ $(date +%s) -lt $END ]]; do
  METRICS=$(kubectl -n $NS exec "$WEB_POD" -c gluetun -- \
    wget -qO- -T 4 http://127.0.0.1:9192/metrics 2>/dev/null)

  CLIENTS=$(awk '/^dispatcharr_active_clients /{print $2}' <<<"$METRICS" | head -1)
  BUF_MIN=$(awk '/^dispatcharr_stream_buffering_speed\{/{print $NF}' <<<"$METRICS" \
            | sort -g | head -1)
  BUF_AVG=$(awk '/^dispatcharr_stream_buffering_speed\{/{s+=$NF; n++} END{if(n) printf "%.3f", s/n; else print "-"}' <<<"$METRICS")
  RATES=$(awk '/^dispatcharr_client_current_transfer_rate_bps\{/{printf "%.1f ", $NF/1000000}' <<<"$METRICS")

  read -r T1 U1 <<<"$(read_throttle)"
  THR_DLT=$(( ${T1:-0} - ${T0:-0} ))
  THR_US=$(( ${U1:-0} - ${U0:-0} ))
  REDIS_RS=$(kubectl -n $NS get pod -l app=dispatcharr,component=redis \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")

  # Flag lines that matter so they are greppable afterwards.
  FLAG=""
  [[ -n "${BUF_MIN:-}" && "$BUF_MIN" != "-" ]] && \
    awk -v b="$BUF_MIN" 'BEGIN{exit !(b<0.95)}' && FLAG="  <-- BUFFERING"
  [[ "$THR_DLT" -gt 0 ]] && FLAG="$FLAG  <-- CPU_THROTTLED"
  [[ "$REDIS_RS" != "$R0" ]] && FLAG="$FLAG  <-- REDIS_RESTARTED"

  printf '%-9s %-8s %-11s %-11s %-9s %-9s %-8s %s%s\n' \
    "$(date +%H:%M:%S)" "${CLIENTS:--}" "${BUF_MIN:--}" "${BUF_AVG:--}" \
    "$THR_DLT" "$THR_US" "$REDIS_RS" "${RATES:--}" "$FLAG"

  sleep "$INTERVAL"
done

echo "--------------------------------------------------------------------------------------"
echo "# end=$(date -Is)"
echo "# Summary: grep -c BUFFERING <logfile>   /   grep -c CPU_THROTTLED <logfile>"
echo "#   BUFFERING but no CPU_THROTTLED  -> look at the tunnel (MTU / DNS / upstream)"
echo "#   BUFFERING with CPU_THROTTLED    -> T1-B, raise or drop the CPU limit"
echo "#   all channels dip together       -> server-side (CPU, redis, tunnel, NIC)"
echo "#   one channel dips alone          -> upstream/provider for that channel"
