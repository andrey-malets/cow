#!/bin/bash

shopt -s nullglob

for i in {1..5}; do
    devices=(/sys/class/iscsi_session/*/device/target*/*/block/*)
    ndevices=${#devices[@]}
    if [[ "$ndevices" -gt 0 ]]; then break; else sleep 1; fi
done

if [[ "$ndevices" -ne 1 ]]; then
    echo "Need exactly one iSCSI dev, but got $ndevices" &>2
    exit 1
fi

ISCSI_DEV=${devices[0]##*/}
ISCSI_SIZE=$(cat "/sys/block/$ISCSI_DEV/size")

red()    { echo "[0;31;40m$1[0;37;40m"; }
green()  { echo "[0;32;40m$1[0;37;40m"; }
yellow() { echo "[0;33;40m$1[0;37;40m"; }

fix_fsmtab() {
    ln -sf /proc/mounts /etc/mtab
    echo -n > /etc/fstab
}

find_space() {
    TARGET_DEV= TARGET_SIZE=0 TARGET_START=0
    local start=0 size=0

    for dev in /sys/block/*; do
        [[ "$dev" == "/sys/block/$ISCSI_DEV" ]] && continue

        local free=1
        if ls $dev/${dev##/sys/block/}* &>/dev/null; then
            for part in $dev/${dev##/sys/block/}*; do
                start=$(cat "$part/start")
                size=$(cat "$part/size")
                if [[ "$((start+size))" -gt "$free" ]]; then
                    free=$((start+size))
                fi
            done
        fi

        size=$(cat "$dev/size")
        if [[ "$((size-free))" -gt "$TARGET_SIZE" ]]; then
            TARGET_DEV=$dev
            TARGET_SIZE=$((size-free))
            TARGET_START=$free
        fi
    done

    if [[ ! -z "$TARGET_DEV" ]] && \
       [[ "$TARGET_SIZE" -gt "$((2*1024*1024*1024/512))" ]]; then # 2 GB
        TARGET_DEV="${TARGET_DEV##/sys/block/}"
        green "find_space: ${TARGET_DEV}, start: $TARGET_START, size: $TARGET_SIZE"
        return 0
    else
        red   "find_space: no suitable disks found"
        return 1
    fi
}

add_part() { dmsetup create "$1" --table "$2"; }

setup_partitions() {
    CONF_SIZE=2047
    COW_SIZE="$((ISCSI_SIZE/2))"
    if [[ "$TARGET_SIZE" -lt "$((1+CONF_SIZE+COW_SIZE))" ]]; then
        COW_SIZE="$((TARGET_SIZE-1-CONF_SIZE))"
    fi

    PLACE_SIZE="$((TARGET_SIZE-1-CONF_SIZE-COW_SIZE))"
    local target="/dev/$TARGET_DEV"

    set -e
        add_part sign "0 1 linear $target $TARGET_START"
        add_part conf "0 $CONF_SIZE linear $target $((TARGET_START+1))"
        add_part cow  "0 $COW_SIZE linear $target $((TARGET_START+1+CONF_SIZE))"
    set +e

    if [[ "$PLACE_SIZE" -ne 0 ]]; then
        set -e
            add_part place "0 $PLACE_SIZE linear \
                $target $((TARGET_START+1+CONF_SIZE+COW_SIZE))"
        set +e
    fi

    green "cow: $COW_SIZE, place: $PLACE_SIZE"
}

gen_conf_sign() {
    dd if=/dev/mapper/conf "count=$CONF_SIZE" \
        2>/dev/null | sha1sum | awk '{print $1}'
}

get_conf_sign() { dd if=/dev/mapper/sign bs=20 count=1 2>/dev/null | xxd -p; }
put_conf_sign() { xxd -r -p | dd of=/dev/mapper/sign bs=20 count=1 2>/dev/null; }

validate_conf_sign() { [[ "$(gen_conf_sign)" == "$(get_conf_sign)" ]]; }

sign_conf() { sync; gen_conf_sign | put_conf_sign; sync; }

add_memcow() {
    local mem=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    modprobe brd "rd_size=$((mem > 500000 ? mem*3/4 : mem/2))" rd_nr=1
}

setup_root() {
    set -e
        add_part root "0 $ISCSI_SIZE snapshot /dev/$ISCSI_DEV $1 P 64"
        kpartx -a /dev/mapper/root
    set +e
}

update_conf() {
    local force_reset=$1
    local mount='mount -t ext2' conf_mp=/tmp/conf conf_part=/dev/mapper/conf
    local rv=0

    update_timestamp() {
        cp /etc/timestamp "$conf_mp"
        umount "$conf_mp"
        sign_conf
        $mount -o ro "$conf_part" "$conf_mp"
    }

    if validate_conf_sign && $mount -o ro "$conf_part" "$conf_mp"; then
        if [[ "$force_reset" -eq 1 ]] || \
         ! cmp "$conf_mp"/timestamp /etc/timestamp &>/dev/null; then
            rv=1
            set -e
                $mount -o rw,remount "$conf_part" "$conf_mp"
                update_timestamp
            set +e
        fi
    else
        red "corrupt config partition, resetting"
        rv=1
        set -e
            mke2fs "$conf_part" &>/dev/null
            $mount "$conf_part" "$conf_mp"
            update_timestamp
        set +e
    fi
    return "$rv"
}

fix_fsmtab

if [[ "$cowtype" == 'mem' ]]; then
    green "forcing memory cow device"
    add_memcow
    COW_ROOT=/dev/ram0
else
    find_space
    if [[ "$?" -eq 0 ]]; then
        setup_partitions

        mkdir /tmp/conf

        [[ "$cowtype" != 'clear' ]]
        need_reset=$?

        update_conf
        conf_updated=$?

        if [[ "$need_reset" -ne 0 ]] || [[ "$conf_updated" -ne 0 ]]; then
            yellow "resetting cow partition"
            dd if=/dev/zero of=/dev/mapper/cow count=1 conv=notrunc 2>/dev/null
            sync
        fi

        COW_ROOT=/dev/mapper/cow
    else
        yellow "falling back to memory cow device"
        add_memcow
        COW_ROOT=/dev/ram0
    fi
fi

setup_root "$COW_ROOT"
