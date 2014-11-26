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

if [[ "$(ls -1d "$syspath"/eth* | wc -l)" -lt 2 ]]; then
    echo 'Skipping network interface selection'
else
    put_up
    for speed in 1000 100; do
        for i in 1 2 3 4 5; do
            for ifpath in "$syspath"/eth*; do
                if [[ "$(cat $ifpath/speed)" -eq "$speed" ]]; then
                    local iface="${ifpath##*/}"
                    echo "Selected $iface at speed $speed"
                    echo "DEVICE=$iface" >> /conf/initramfs.conf
                    put_down_except "$iface"
                    exit 0
                fi
            done
            sleep 1
        done
    done

    # fallback
    echo 'No interface selected'
    exit 1
fi
