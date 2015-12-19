#!/usr/bin/env bash

set -x

BASE=$(dirname "$0")
PATH=/usr/sbin:/usr/bin:/sbin:/bin
HOST_CONFIG=${1:?no host config file}
IMAGE_CONFIG=${2:?no image config file}

for CONFIG in "$HOST_CONFIG" "$IMAGE_CONFIG"; do
    if [[ ! -r "$CONFIG" ]]; then
      echo "cannot read $CONFIG" 1>&2
      exit 1
    else
      . "$CONFIG"
    fi
done

config_name() {
    local base_name=$(basename "$1")
    echo "${base_name%%.sh}"
}

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

domain_shuts_down() {
    xl list | tail -n+2 | awk '{print $1}' | grep -qw "$1"
    [[ "$?" -eq 1 ]]
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
xl shutdown -w "$REF_VM_NAME"
wait_for 5 domain_shuts_down "$REF_VM_NAME"

wait_for 5 volume_closed "$REF_VM_DISK"
if [[ "$?" -ne 0 ]]; then
  echo "timed out while waiting for $REF_VM_DISK to free" 1>&2
  echo "starting $REF_VM_NAME back" 1>&2
  xl create "$REF_VM_PATH"
  exit 1
else
  echo "adding snapshot $SNAPSHOT_FILENAME"
  lvcreate -L "$SNAPSHOT_SIZE" -s -n "$SNAPSHOT_FILENAME" "$REF_VM_DISK"
fi

echo "starting $REF_VM_NAME back" 1>&2
xl create "$REF_VM_PATH"

check_partition_table_type() {
  [[ "$(parted -m "$1" p | head -n2 | tail -n1 | cut -f6 -d:)" == gpt ]]
}

if ! check_partition_table_type "$REF_VM_DISK"; then
  echo "ref vm must have GPT disk" 1>&2
  lvremove -f "$SNAPSHOT_FILENAME"
  exit 1
fi

get_partition_number() {
  local volume=$1
  output=$(parted -m "$volume" p | tail -n+3 | grep "${PARTITION_NAMES[base]}")
  if [[ -z "$output" ]] || [[ "$(echo -n "$output" | wc -l)" -ne 0 ]]; then
    echo "there should be exactly one ${PARTITION_NAMES[base]}" \
         "on $REF_VM_DISK" 1>&2
    return 1
  fi
  cut -f1 -d: <<< "$output"
}

get_kpartx_name() {
  local volume=$1
  number=$(get_partition_number "$volume")
  if [[ "$?" -ne 0 ]]; then
    return 1
  else
    kpartx -l "$volume" | tail -n+"$number" | head -n1 | cut -f1 -d' '
  fi
}

PARTITION_NUMBER=$(get_partition_number "$SNAPSHOT_FILENAME")
if [[ "$?" -ne 0 ]]; then
  echo "failed to get partition number for $SNAPSHOT_FILENAME" 1>&2
  lvremove -f "$SNAPSHOT_FILENAME"
  exit 1
fi

KPARTX_NAME=$(get_kpartx_name "$SNAPSHOT_FILENAME")
if [[ "$?" -ne 0 ]]; then
  echo "failed to get kpartx name for $SNAPSHOT_FILENAME" 1>&2
  lvremove -f "$SNAPSHOT_FILENAME"
  exit 1
fi

KPARTX_FILENAME="/dev/mapper/$KPARTX_NAME"

if ! parted "$SNAPSHOT_FILENAME" \
    name "$PARTITION_NUMBER" "${PARTITION_NAMES[network]}"; then
  echo "failed to change partition name for $SNAPSHOT_FILENAME" 1>&2
  lvremove -f "$SNAPSHOT_FILENAME"
  exit 1
fi

kpartx -s -v -a "$SNAPSHOT_FILENAME"
if ! [[ -e "$SNAPSHOT_FILENAME" ]]; then
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
PREREQS=(root/cow/{fake,prereqs.sh})
for PREREQ in "${PREREQS[@]}"; do
    mkdir -p "$MOUNT_DIR"/"$(dirname "$PREREQ")"
    cp -a "$TO_COPY_DIR"/"$PREREQ" "$MOUNT_DIR"/"$(dirname "$PREREQ")"
done
chroot "$MOUNT_DIR" "/root/cow/prereqs.sh"

echo 'copying files'
TO_COPY_DIR="$BASE/tocopy"
cp -a "$TO_COPY_DIR"/* "$MOUNT_DIR"

echo 'performing target configuration'
echo "$TIMESTAMP" > "$MOUNT_DIR"/etc/timestamp
cat >> "$MOUNT_DIR"/etc/iscsi/iscsi.initramfs <<END
ISCSI_TARGET_IP=$TARGET_HOST
ISCSI_TARGET_PORT=$ISCSI_TARGET_PORT
ISCSI_TARGET_NAME=$ISCSI_TARGET_NAME
END

{
    echo "declare -A PARTITION_NAMES"
    for name in "${!PARTITION_NAMES[@]}"; do
        echo "PARTITION_NAMES[$name]=${PARTITION_NAMES[$name]}"
    done
} > "$MOUNT_DIR"/etc/cow.conf

echo 'running update script'
CHROOT_SCRIPT=/root/cow/update.sh
chroot "$MOUNT_DIR" "$CHROOT_SCRIPT"

WEB_TARGET="$WEB_PATH/$(config_name "$IMAGE_CONFIG")"
for file in vmlinuz initrd.img; do
    link=$(readlink "$MOUNT_DIR/$file")
    cp "$MOUNT_DIR/${link##/}" "$WEB_TARGET/$file"
done
echo "$TIMESTAMP" > "$WEB_TARGET/timestamp"
chroot "$MOUNT_DIR" dpkg-query -W -f='${Package}(${Version}) ' > "$WEB_TARGET/pkgs"

echo 'cleaning up'
umount "$MOUNT_DIR"/{proc,sys,dev/pts,dev,}
kpartx -v -d "$SNAPSHOT_FILENAME"

echo 'updating iet targets'
"$BASE/iet.py" "$CONFIG"

echo 'publishing symlink to root partition for deployment'
kpartx -s -v -a "$SNAPSHOT_FILENAME"
DEPLOY_TARGET="$WEB_TARGET/${PARTITION_NAMES[local]}"
ln -sf "$KPARTX_FILENAME" "$DEPLOY_TARGET"

echo 'rebooting test host'
TEST_VM_NAME=${TEST_VM_PATH##*/}
xl shutdown -w "$TEST_VM_NAME"
wait_for 5 domain_shuts_down "$TEST_VM_NAME"
xl create "$TEST_VM_PATH"
