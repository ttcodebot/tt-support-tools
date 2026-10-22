#!/usr/bin/env bash
# Usage: precheck-projects.sh <tools_dir> <tech> <projects_dir> <out_dir> <project>...
# Runs precheck on each project and writes <out_dir>/<project>.md plus summary.md.
# Must run inside `nix-shell` (for klayout) with PDK and PDK_ROOT set.
set -uo pipefail
TOOLS=$(realpath "$1"); TECH=$2; PROJECTS=$(realpath "$3"); OUT=$(realpath -m "$4"); shift 4
mkdir -p "$OUT"
pass=0; fail=0
for name in "$@"; do
  dir="$PROJECTS/$name"
  layout=$(ls "$dir/$name.oas" "$dir/$name.gds" 2>/dev/null | head -1)
  rm -f "$TOOLS/precheck/reports/results.md"
  start=$(date +%s)
  (cd "$TOOLS/precheck" && timeout 1800 python precheck.py --gds "$layout" --tech "$TECH") > "$OUT/$name.log" 2>&1
  rc=$?
  cp "$TOOLS/precheck/reports/results.md" "$OUT/$name.md" 2>/dev/null || echo "no results.md (rc=$rc)" > "$OUT/$name.md"
  if [ $rc -eq 0 ]; then pass=$((pass+1)); s=PASS; else fail=$((fail+1)); s=FAIL; fi
  echo "$s $name ($(( $(date +%s) - start ))s)" | tee -a "$OUT/summary.md"
done
echo "passed=$pass failed=$fail" | tee -a "$OUT/summary.md"
