#!/bin/bash

shopt -s nullglob

. /etc/cow.conf
part_name() {
    echo "/dev/disk/by-partlabel/${PARTITION_NAMES[$1]}"
}

if [[ "$cowsrc" == network ]]; then
    BASE=$(part_name network)
else
    BASE=$(part_name local)
fi
DISK_COW=$(part_name cow)
CONF=$(part_name conf)
SIGN=$(part_name sign)

for i in {1..5}; do
    if [[ -b "$BASE" ]]; then break; else sleep 1; fi
done

if ! [[ -b "$BASE" ]]; then
    echo "Timed out waiting for $BASE"
    exit 1
fi

red()    { echo "[0;31;40m$1[0;37;40m"; }
green()  { echo "[0;32;40m$1[0;37;40m"; }
yellow() { echo "[0;33;40m$1[0;37;40m"; }

fix_fsmtab() {
    ln -sf /proc/mounts /etc/mtab
    echo -n > /etc/fstab
}

add_part() { dmsetup create "$1" --table "$2"; }

gen_conf_sign() {
    dd "if=$CONF" 2>/dev/null | sha1sum | awk '{print $1}'
}

get_conf_sign() { dd "if=$SIGN" bs=20 count=1 2>/dev/null | xxd -p; }
put_conf_sign() { xxd -r -p | dd of="$SIGN" bs=20 count=1 2>/dev/null; }

validate_conf_sign() { [[ "$(gen_conf_sign)" == "$(get_conf_sign)" ]]; }

sign_conf() { sync; gen_conf_sign | put_conf_sign; sync; }

try_conf_from_image() {
    local mp=/tmp/conf
    mkdir -p "$mp"

    local conf_uuid=5c4b0ee9-e5b6-44ce-9247-43103b07a95a
    local conf_part=$(blkid -U "$conf_uuid")
    if [[ "$?" -eq 0 ]]; then
        local image=/tmp/conf_image size=256K
        dd "if=$conf_part" "of=$image" "bs=$size" count=1
        modprobe loop
        mount -o loop "$image" "$mp"
    fi
}

setup_memcow() {
    local mem=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    modprobe brd "rd_size=$((mem > 500000 ? mem*3/4 : mem/2))" rd_nr=1
    try_conf_from_image
}

setup_root() {
    local base=$1 snapshot=$2
    local size=$(blockdev --getsize64 "$base")
    set -e
        add_part root "0 $((size/512)) snapshot $base $snapshot P 64"
    set +e
}

update_conf() {
    local force_reset=$1
    local mount=(mount -t ext2)
    local conf_mp=/tmp/conf
    local rv=0

    update_timestamp() {
        cp /etc/timestamp "$conf_mp"
        umount "$conf_mp"
        sign_conf
        "${mount[@]}" -o ro "$CONF" "$conf_mp"
    }

    if validate_conf_sign && "${mount[@]}" -o ro "$CONF" "$conf_mp"; then
        if [[ "$force_reset" -eq 1 ]] || \
         ! cmp "$conf_mp"/timestamp /etc/timestamp &>/dev/null; then
            rv=1
            set -e
                "${mount[@]}" -o rw,remount "$CONF" "$conf_mp"
                update_timestamp
            set +e
        fi
    else
        red "corrupt config partition, resetting"
        rv=1
        set -e
            mke2fs "$CONF" &>/dev/null
            "${mount[@]}" "$CONF" "$conf_mp"
            update_timestamp
        set +e
    fi
    return "$rv"
}

check_partitions() {
    rv=0
    for part in "$DISK_COW" "$CONF" "$SIGN"; do
        if ! [[ -b "$part" ]]; then
            yellow "$part not found"
            rv=1
        fi
    done
    return "$rv"
}

fix_fsmtab

if [[ "$cowtype" == 'mem' ]]; then
    green "forcing memory cow device"
    setup_memcow
    COW=/dev/ram0
else
    if check_partitions; then
        mkdir -p /tmp/conf

        [[ "$cowtype" != 'clear' ]]
        need_reset=$?

        update_conf
        conf_updated=$?

        if [[ "$need_reset" -ne 0 ]] || [[ "$conf_updated" -ne 0 ]]; then
            yellow "resetting cow partition"
            dd if=/dev/zero "of=$DISK_COW" count=1 conv=notrunc 2>/dev/null
            sync
        fi

        COW=$DISK_COW
    else
        yellow "falling back to memory cow device"
        setup_memcow
        COW=/dev/ram0
    fi
fi

setup_root "$BASE" "$COW"
