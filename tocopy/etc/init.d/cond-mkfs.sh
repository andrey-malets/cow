#! /bin/sh
### BEGIN INIT INFO
# Provides:          cond-mkfs
# Required-Start:    checkroot
# Required-Stop:
# Default-Start:     S
# Default-Stop:
# Short-Description: Make /place filesystem if it does not exist
# Description:       Conditionally make filesystem for /place
#                    if it didn't exist before
### END INIT INFO

case "$1" in
    start|"")
        DEVICE=/dev/mapper/place
        MP=/place

        [ -e "$DEVICE" ] || { echo "$DEVICE does not exist, quit"; exit 0; }

        echo "Checking if filesystem exists on $DEVICE..."
        e2fsck -v -p "$DEVICE"
        case "$?" in
            0|1)
                echo "OK, nothing to do"
            ;;
            2|4|8)
                echo "Some really bad shit happened, doing mkfs"
                mkfs.ext4 "$DEVICE"
            ;;
            *)
                echo "Unrecoverable error, giving up"
                exit 1
            ;;
        esac

        echo "Mounting $DEVICE on $MP"
        mkdir -p "$MP"
        mount "$DEVICE" "$MP"

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
        # No-op
        ;;
        *)
        echo "Usage: cond-mkfs.sh [start|stop]" >&2
        exit 3
    ;;
esac
