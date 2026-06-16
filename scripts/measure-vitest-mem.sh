#!/usr/bin/env bash
# measure-vitest-mem.sh — true peak memory of a vitest run on macOS.
#
# `/usr/bin/time -l` only reports the parent process's peak RSS, which misses the
# memory living in vitest's worker forks. This samples the RSS of ALL vitest
# processes once per second and reports the peak SUM across the whole tree.
#
# Usage (run from inside your repo):
#   bash measure-vitest-mem.sh                       # defaults to: npx vitest run --logHeapUsage
#   bash measure-vitest-mem.sh npx vitest run        # or pass your own command
set -uo pipefail

CMD=("$@")
[ ${#CMD[@]} -eq 0 ] && CMD=(npx vitest run --logHeapUsage)

echo "running: ${CMD[*]}"
"${CMD[@]}" &
RUN_PID=$!

peak_kb=0
peak_n=0
while kill -0 "$RUN_PID" 2>/dev/null; do
  pids=$(pgrep -f vitest 2>/dev/null | tr '\n' ' ')
  if [ -n "${pids// }" ]; then
    read -r total n < <(ps -o rss= -p ${pids// /,} 2>/dev/null \
      | awk '{s+=$1; c++} END{print (s+0), (c+0)}')
    if [ "${total:-0}" -gt "$peak_kb" ]; then peak_kb=$total; peak_n=$n; fi
  fi
  sleep 1
done
wait "$RUN_PID"; status=$?

awk -v kb="$peak_kb" -v n="$peak_n" 'BEGIN{
  printf "\n=== PEAK total RSS across vitest processes: %.2f GB  (%d procs at peak) ===\n", kb/1048576, n
}'
echo "(exit code: $status)"
exit "$status"
