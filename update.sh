#!/bin/bash

# set -x

BASE=$(dirname "$0")
PATH=/usr/sbin:/usr/bin:/sbin:/bin
CONFIG=${1:?no config file}

if [[ ! -r "$CONFIG" ]]; then
  echo "cannot read $CONFIG" 1>&2
  exit 1
else
  . "$CONFIG"
fi

get_ref_vm_disk_filename() {
  if [[ ! -r "$REF_VM_PATH" ]]; then
    echo "cannot read ref vm config file at $REF_VM_PATH" 1>&2
    return 1
  fi

  local disks=()
  disks=($(python "$BASE/get_disks.py" "$REF_VM_PATH"))
  if [[ "$?" -ne 0 ]]; then
    echo 'failed to get disk filename for ref vm' 1>&2
    return 1
  fi

  if [[ ${#disks[@]} -ne 1 ]]; then
    echo "ref vm should have exactly one phy:/ disk, but it has ${#disks[@]}" 1>&2
    return 1
  fi
  echo "${disks[0]}"
}

get_timestamp() {
  echo "${TIMESTAMP_SUFFIX}$(date '+%F_%H-%M-%S')"
}

REF_VM_DISK=$(get_ref_vm_disk_filename)
if [[ "$?" -ne 0 ]]; then
  exit 1
fi

if [[ ! -r "$REF_VM_DISK" ]]; then
  echo "$REF_VM_DISK in unreadable"
  exit 1
fi

TIMESTAMP=$(get_timestamp)

SNAPSHOT_FILENAME="${REF_VM_DISK}-${TIMESTAMP}"
SNAPSHOT_BASENAME=$(basename $SNAPSHOT_FILENAME)

if [[ -e "$SNAPSHOT_FILENAME" ]]; then
  echo "$SNAPSHOT_FILENAME already exists" 1>&2
  exit 1
fi

ISCSI_TARGET_NAME="$SNAPSHOT_BASENAME"

MOUNT_DIR="$BASE/root"
TO_COPY_DIR="$BASE/tocopy"

REF_VM_NAME=${REF_VM_PATH##*/}

volume_closed() {
  local volume=$1
  local attrs=($(lvs -o lv_attr --noheadings "$volume"))
  [[ "${attrs:5:1}" == '-' ]]
}

wait_for() {
  local tries=$1
  shift
  for i in $(seq "$tries"); do
    "$@" && return 0
    [[ "$i" -eq "$tries" ]] || sleep 1
  done
  return 1
}

echo "shutting down $REF_VM_NAME"
xm shutdown -w "$REF_VM_NAME"

wait_for 3 volume_closed "$REF_VM_DISK"
if [[ "$?" -ne 0 ]]; then
  echo "timed out while waiting for $REF_VM_DISK to free" 1>&2
  echo "starting $REF_VM_NAME back" 1>&2
  xm create "$REF_VM_NAME"
  exit 1
else
  echo "adding snapshot $SNAPSHOT_FILENAME"
  lvcreate -L "$SNAPSHOT_SIZE" -s -n "$SNAPSHOT_FILENAME" "$REF_VM_DISK"
fi

echo "starting $REF_VM_NAME back" 1>&2
xm create "$REF_VM_PATH"

get_kpartx_name() {
  local volume=$1
  if [[ "$(kpartx -l "$volume" | wc -l)" -ne 1 ]]; then
    echo "only single-volume VMs are supported" 1>&2
    return 1
  else
    kpartx -l "$volume" | cut -f1 -d' '
  fi
}

KPARTX_NAME=$(get_kpartx_name "$SNAPSHOT_FILENAME")
if [[ "$?" -ne 0 ]]; then
  echo "failed to get kpartx name for $SNAPSHOT_FILENAME" 1>&2
  lvremove -f "$SNAPSHOT_FILENAME"
  exit 1
fi

KPARTX_FILENAME="/dev/mapper/$KPARTX_NAME"

kpartx_volume() {
  [[ -e "$1" ]]
}

kpartx -v -a "$SNAPSHOT_FILENAME"
if [[ "$?" -ne 0 ]] || ! wait_for 3 kpartx_volume "$KPARTX_FILENAME"; then
  echo "something wrong with kpartx" 1>&2
  kpartx -d "$SNAPSHOT_FILENAME"
  lvremove -f "$SNAPSHOT_FILENAME"
  exit 1
fi

echo "mounting $KPARTX_FILENAME and auxiliary filesystems"

MOUNT_DIR="$BASE/root"

mount "$KPARTX_FILENAME" "$MOUNT_DIR"
mkdir -p "$MOUNT_DIR"/{proc,sys,dev}

for fs in proc sys dev dev/pts; do
  mount --bind /"$fs" "$MOUNT_DIR"/"$fs"
done

echo 'installing prerequisites'
PREREQS_SCRIPT=root/prereqs.sh
cp -a "$TO_COPY_DIR"/"$PREREQS_SCRIPT" \
    "$MOUNT_DIR"/"$(dirname "$PREREQS_SCRIPT")"
chroot "$MOUNT_DIR" "/$PREREQS_SCRIPT"

echo 'copying files'
TO_COPY_DIR="$BASE/tocopy"
cp -a "$TO_COPY_DIR"/* "$MOUNT_DIR"

echo 'performing target configuration'
echo "$TIMESTAMP" > "$MOUNT_DIR"/etc/timestamp
cat >> "$MOUNT_DIR"/etc/iscsi/iscsi.initramfs <<END
ISCSI_TARGET_IP=$ISCSI_TARGET_HOST
ISCSI_TARGET_NAME=$ISCSI_TARGET_NAME
END

echo 'running update script'
CHROOT_SCRIPT=/root/update.sh
chroot "$MOUNT_DIR" "$CHROOT_SCRIPT"

mkdir -p "$WEB_PATH"
for file in vmlinuz initrd.img; do
    link=$(readlink "$MOUNT_DIR/$file")
    cp "$MOUNT_DIR/${link##/}" "$WEB_PATH/$file"
done
echo "$TIMESTAMP" > "$WEB_PATH/timestamp"
chroot "$MOUNT_DIR" dpkg-query -W -f='${Package}(${Version}) ' > "$WEB_PATH/pkgs"

echo 'cleaning up'
umount "$MOUNT_DIR"/{proc,sys,dev/pts,dev,}
kpartx -v -d "$SNAPSHOT_FILENAME"

echo 'updating iet targets'
"$BASE/iet.py" "$CONFIG"

echo 'rebooting test host'
TEST_VM_NAME=${TEST_VM_PATH##*/}
xm shutdown -w "$TEST_VM_NAME"
xm create "$TEST_VM_PATH"