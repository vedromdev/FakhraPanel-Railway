#!/bin/sh
# Fake xray for tests: `-test` validates the config, `run` stays alive.
# Rejected configs are signalled by an "INVALID_CONFIG" marker in the file.

cfg=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-c" ]; then
        cfg=$arg
    fi
    prev=$arg
done

case " $* " in
    *" -test "*)
        if [ ! -f "$cfg" ]; then
            echo "fake-xray: config file not found: $cfg" >&2
            exit 1
        fi
        if grep -q '"INVALID_CONFIG"' "$cfg"; then
            echo "fake-xray: config test failed: INVALID_CONFIG marker present" >&2
            exit 1
        fi
        echo "fake-xray: config OK"
        exit 0
        ;;
esac

# run mode: a config that passed -test may still be asked to die immediately
if grep -q '"DIE_ON_RUN"' "$cfg"; then
    echo "fake-xray: dying on run as instructed" >&2
    exit 1
fi

# run mode: pretend to serve until told to stop
trap 'exit 0' TERM INT
while true; do sleep 1; done
