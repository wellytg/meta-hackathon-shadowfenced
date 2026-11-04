#!/usr/bin/env bash
# Start Shadowfenced locally: run the doctor, pick a profile, launch uvicorn.
#
# Usage:
#   ./setup/start.sh          # use the doctor's recommended profile
#   ./setup/start.sh light    # force full | light | code
#
# Sets SF_JUDGE_MODEL for the full/light profiles, starts uvicorn on
# 127.0.0.1:8765, and prints the URL. Code profile needs no judge model.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROFILE="${1:-}"
if [ -n "$PROFILE" ] && [ "$PROFILE" != "full" ] && [ "$PROFILE" != "light" ] && [ "$PROFILE" != "code" ]; then
    echo "Unknown profile '$PROFILE'. Use one of: full, light, code."
    exit 1
fi

# 1. Doctor first: show the machine report (never prints key values).
echo "--- doctor ---"
python3 setup/check.py || {
    echo "Python is too old (need >= 3.11). Stopping."
    exit 1
}

# 2. Profile: an explicit argument wins, else the doctor's recommendation.
if [ -z "$PROFILE" ]; then
    PROFILE="$(python3 setup/check.py --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"]["profile"])')"
    echo "--- profile: $PROFILE (doctor's recommendation) ---"
else
    echo "--- profile: $PROFILE (your choice) ---"
fi

# 3. Judge model for the profile.
case "$PROFILE" in
    full)  export SF_JUDGE_MODEL="muse-glimmer:30b" ;;
    light) export SF_JUDGE_MODEL="llama3.2:3b" ;;
    code)  ;;
esac
if [ "$PROFILE" = "code" ]; then
    echo "Code-only profile: no judge model needed (hard cases are held)."
else
    echo "SF_JUDGE_MODEL=$SF_JUDGE_MODEL"
fi

# 4. Launch. The server must keep running: if it exits within the first
# seconds it never came up, so say so and exit non-zero instead of
# reporting success. Its own error output streams above this line.
echo "Starting Shadowfenced on http://127.0.0.1:8765 ..."
echo "Open that URL in your browser. Stop with Ctrl+C."
STARTED=$SECONDS
python3 -m uvicorn shadowfenced.app:app --app-dir src --host 127.0.0.1 --port 8765
CODE=$?
ELAPSED=$((SECONDS - STARTED))
if [ "$ELAPSED" -lt 10 ]; then
    if [ "$CODE" -eq 0 ]; then CODE=1; fi
    echo "Shadowfenced exited after ${ELAPSED}s with exit code $CODE: it did not start. Quote the error above when reporting."
    exit "$CODE"
fi
exit "$CODE"
