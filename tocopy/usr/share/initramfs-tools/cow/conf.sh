#!/bin/bash

. /etc/cow.conf
CONF="/dev/disk/by-partlabel/${PARTITION_NAMES[conf]}"

remount() { mount -o "$1,remount" "$rootmnt"; }
RW=(remount rw)
RO=(remount ro)

"${RW[@]}"
cp /etc/resolv.conf "$rootmnt/etc/"
"${RO[@]}"

if [[ -b "$CONF" ]]; then
    puppet="$rootmnt/var/lib/puppet"
    if [[ -d "$puppet" ]]; then
        . /run/net-*.conf

        fqdn="$HOSTNAME.$DNSDOMAIN"
        files=("certs/ca.pem 771" "certs/$fqdn.pem 755"
               "private_keys/$fqdn.pem 750")

        present=1
        for spec in "${files[@]}"; do
            [[ -f "/tmp/conf/puppet/${spec%% *}" ]] || present=0
        done

        if [[ "$present" -eq 1 ]]; then
            "${RW[@]}"
            (
                set -e
                for spec in "${files[@]}"; do
                    file=${spec%% *} mode=${spec##* }
                    src="/tmp/conf/puppet/$file"
                    dst="$puppet/ssl/$file"
                    mkdir -p "${dst%/*}"
                    chmod "$mode" "${dst%/*}"
                    cp "$src" "$dst"
                done
                rm -f "$puppet/state/agent_disabled.lock"
            )
            "${RO[@]}"
        fi
    fi
    umount /tmp/conf
fi
