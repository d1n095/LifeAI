#!/bin/bash
# UI regressions (D4/D5) against a live local stack. Usage: run.sh <candidate-worktree> <built-frontend-dir>
# Starts proxy_v1/live_stack.sh, copies the spec into the SCRATCH frontend copy (never the frozen worktree), runs Playwright,
# prints pytest-style lines, tears the stack down. Needs Playwright Chromium in <built-frontend-dir>/../pw-browsers.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; FE="$(cd "$2" && pwd)"
"$HERE/../proxy_v1/live_stack.sh" up "$TARGET" "$FE" >/dev/null 2>&1 < /dev/null || { echo "FAILED ui::STACK_DID_NOT_START"; exit 1; }
mkdir -p "$FE/examiner-ui" && cp "$HERE/founder_status.spec.ts" "$HERE/playwright.examiner.config.ts" "$FE/examiner-ui/"
OUT="$(cd "$FE" && PLAYWRIGHT_BROWSERS_PATH="$FE/../pw-browsers" NEXT_TELEMETRY_DISABLED=1 npx playwright test -c examiner-ui/playwright.examiner.config.ts 2>&1)"
RC=$?
echo "$OUT" | grep -E "✓|✘|-  |passed|failed|skipped|Error:" | head -40
if echo "$OUT" | grep -q "Executable doesn't exist\|browserType.launch"; then
  echo "FAILED ui::SETUP_BROWSER_MISSING (results for UI-1..3 are not evidence)"; "$HERE/../proxy_v1/live_stack.sh" down >/dev/null 2>&1 < /dev/null; exit 1
fi
for id in UI-1 UI-2 UI-3; do
  line="$(echo "$OUT" | grep -E "(✓|✘|-) .*$id" | head -1)"
  case "$line" in *✓*) echo "PASSED ui::$id";; *✘*) echo "FAILED ui::$id";; *) echo "SKIPPED ui::$id";; esac
done
"$HERE/../proxy_v1/live_stack.sh" down >/dev/null 2>&1 < /dev/null; exit $RC
