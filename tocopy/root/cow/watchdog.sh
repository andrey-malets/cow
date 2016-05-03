#!/usr/bin/env bash

if ! grep -q 'cowsrc=network' /proc/cmdline; then
    exit 0
fi

if ! dmesg | grep -q 'detected conn error'; then
    exit 0
fi

exit 1
