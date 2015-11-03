#!/bin/bash

. /etc/cow.conf
CONF="/dev/disk/by-partlabel/${PARTITION_NAMES[conf]}"
SIGN="/dev/disk/by-partlabel/${PARTITION_NAMES[sign]}"

if [[ -b "$CONF" ]] && [[ -b "$SIGN" ]] && \
        ! grep "$(readlink -f $CONF)" /proc/mounts; then
    CONF_MNT=$(mktemp -d)
    if mount "$CONF" "$CONF_MNT"; then
        if [[ "$#" -eq 1 ]]; then
            "$1" "$CONF_MNT"
        else
            declare -a cmdline
            for ((i=1; i != $#+1; ++i)); do
                cmdline[i-1]="${!i/\{\}/$CONF_MNT}"
            done
            "${cmdline[@]}"
        fi
        rv=$?
        umount "$CONF_MNT"
        rmdir "$CONF_MNT"

        sync
        dd "if=$CONF" 2>/dev/null | \
            sha1sum | cut -f1 -d' ' | xxd -r -p | \
            dd "of=$SIGN" bs=20 count=1 2>/dev/null
        sync

        exit "$rv"
    else
        rmdir "$CONF_MNT"
        exit 20
    fi
else
    exit 10
fi
