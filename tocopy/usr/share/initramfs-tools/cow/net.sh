#!/bin/bash

syspath=/sys/class/net

put_up() {
    # raise up all interfaces
    for ifpath in "$syspath"/eth*; do
        local iface="${ifpath##*/}"
        echo "Putting $iface up"
        ifconfig "$iface" up
    done
}

put_down_except() {
    local up="$1"
    # shut down all interfaces except one
    for ifpath in "$syspath"/eth*; do
        iface="${ifpath##*/}"
        if [[ "$iface" != "$up" ]]; then
            echo "Shutting down $iface"
            ifconfig "${iface}" down
        fi
    done
}

select_iface() {
    echo "Selected $1 $2"
    echo "DEVICE=$1" >> /conf/initramfs.conf
    put_down_except "$1"
    exit 0
}

if [[ "$(ls -1d "$syspath"/eth* | wc -l)" -lt 2 ]]; then
    echo 'Skipping network interface selection'
else
    put_up
    if [[ -n "$bootif_mac" ]]; then
        for ifpath in "$syspath"/eth*; do
            addr=$(cat "$ifpath/address")
            if [[ "${addr,,}" == "${bootif_mac,,}" ]]; then
                select_iface "${ifpath##*/}" "with ${addr,,}"
            fi
        done
    else
        for speed in 1000 100; do
            for _ in 1 2 3 4 5; do
                for ifpath in "$syspath"/eth*; do
                    if [[ "$(cat "$ifpath/speed")" -eq "$speed" ]]; then
                        select_iface "${ifpath##*/}" "at speed $speed"
                    fi
                done
                sleep 1
            done
        done
    fi

    # fallback
    echo 'No interface selected'
    exit 1
fi

if [[ "$cowsrc" != network ]]; then
    echo 'Booting locally: resetting iSCSI config, configuring network'
    echo -n > /etc/iscsi.initramfs
    . /scripts/functions
    configure_networking
fi
