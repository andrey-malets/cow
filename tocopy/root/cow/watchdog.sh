#!/usr/bin/env bash

if ! grep -q 'cowsrc=network' /proc/cmdline; then
    exit 0
fi

if dmesg | grep -q 'session recovery timed out'; then
    dmesg -T | mail -s "FAILURE: iSCSI session did not recover!" root
    sleep 3
    exit 1
elif iscsiadm -m session -P 3 | grep -q 'iSCSI Session State: FAILED'; then
    (iscsiadm -m session -P 3; dmesg -T) | mail -s "WARNING: iSCSI session failure" root
    sleep 3
    exit 0
else
    # dmesg -T | mail -s "I'm alive" root
    exit 0
fi

exit 1
