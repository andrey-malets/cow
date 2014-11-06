#!/bin/bash

if [[ -e /dev/mapper/conf ]] && \
   [[ -e /dev/mapper/sign ]] && \
 ! grep /dev/mapper/conf /proc/mounts; then
    CONF=$(mktemp -d)
    if mount /dev/mapper/conf "$CONF"; then
        if [[ "$#" -eq 1 ]]; then
            "$1" "$CONF"
        else
            declare -a cmdline
            for ((i=1; i != $#+1; ++i)); do
                cmdline[i-1]="${!i/\{\}/$CONF}"
            done
            "${cmdline[@]}"
        fi
        rv=$?
        umount "$CONF"
        rmdir "$CONF"

        sync
        dd if=/dev/mapper/conf count=2047 2>/dev/null | \
            sha1sum | cut -f1 -d' ' | xxd -r -p | \
            dd of=/dev/mapper/sign bs=20 count=1 2>/dev/null
        sync

        exit "$rv"
    else
        rmdir "$CONF"
        exit 20
    fi
else
    exit 10
fi
