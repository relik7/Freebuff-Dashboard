#!/bin/sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    PYTHON=python
fi
if [ ! -f "tools/make_test_profile.py" ]; then
    echo "run-mock.sh: tools/make_test_profile.py is missing." >&2
    echo "Restore the complete handoff archive before running the mock." >&2
    exit 1
fi
echo
echo "Refreshing the mock Freebuff profile for this folder..."
"$PYTHON" "tools/make_test_profile.py" --root "testprofile" --port 8771 || {
    echo
    echo "run-mock.sh: could not create the mock profile." >&2
    exit 1
}
echo
echo "Mock data : $(pwd)/testprofile/.config/freebuff-desktop"
echo "Mock URL  : http://127.0.0.1:8771"
echo "Real Freebuff profiles are not used."
echo
exec "$PYTHON" -u "fb-dashboard.py" --config "testprofile/config.json" "$@"
