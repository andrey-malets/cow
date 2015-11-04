#!/usr/bin/env bash
### BEGIN INIT INFO
# Provides:          cond-mkfs
# Required-Start:    checkroot
# Required-Stop:
# Default-Start:     S
# Default-Stop:
# Short-Description: Make /place filesystem if it does not exist
# Description:       Conditionally make filesystem for /place
#                    if it didn't exist before and mount it on /place
### END INIT INFO

. /etc/cow.conf
PLACE="/dev/disk/by-partlabel/${PARTITION_NAMES[place]}"
MP=/place

case "$1" in
    start|"")
        [[ -b "$PLACE" ]] || { echo "$PLACE does not exist, quit"; exit 0; }

        echo "Checking if filesystem exists on $PLACE"
        e2fsck -v -p "$PLACE"
        case "$?" in
            0|1)
                echo "OK, nothing to do"
            ;;
            2|4|8)
                echo "Some really bad shit happened, doing mkfs"
                mkfs.ext4 "$PLACE"
            ;;
            *)
                echo "Unrecoverable error, giving up"
                exit 1
            ;;
        esac

        echo "Mounting $PLACE on $MP"
        mkdir -p "$MP"
        mount "$PLACE" "$MP"

        # this is 'place' group in AD
        chown root:10010 "$MP"
        chmod +t,ug+rwx,o-rw,o+x "$MP"
        exit 0
    ;;

    restart|reload|force-reload)
        echo "Error: argument '$1' not supported" >&2
        exit 3
    ;;

    stop)
        if [[ -b "$PLACE" ]]; then umount "$MP"; fi
        ;;
    *)
        echo "Usage: cond-mkfs.sh [start|stop]" >&2
        exit 3
    ;;
esac
