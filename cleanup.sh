#!/usr/bin/env bash

set -xe

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


get_ref_vm_disk_filename() {
  if [[ ! -r "$REF_VM_PATH" ]]; then
    echo "cannot read ref vm config file at $REF_VM_PATH" 1>&2
    return 1
  fi

  local disks=()
  disks=($(python3 "$BASE/get_disks.py" "$REF_VM_PATH"))
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


REF_VM_DISK=$(get_ref_vm_disk_filename)
if [[ "$?" -ne 0 ]]; then
  exit 1
fi

if [[ ! -r "$REF_VM_DISK" ]]; then
  echo "$REF_VM_DISK in unreadable"
  exit 1
fi


REF_BASENAME="$(basename $REF_VM_DISK)"

ISCSI_TARGET_PREFIX=iqn.2013-07.org.urgu.
ISCSI_TARGET_BASENAME="$ISCSI_TARGET_PREFIX$REF_BASENAME-$TIMESTAMP_SUFFIX"

SYS_ISCSI_PATH=/sys/kernel/config/target/iscsi

declare -a targets=($SYS_ISCSI_PATH/$ISCSI_TARGET_BASENAME*)
for ((i = 0; i != ${#targets[@]} - 1; ++i)); do
    if [[ $(wc -l "${targets[i]}/tpgt_1/dynamic_sessions" |
            cut -f1 -d" ") != "0" ]]; then
        continue
    fi

    ISCSI_TARGET_NAME=$(basename "${targets[i]}")
    SNAPSHOT_BASENAME=${ISCSI_TARGET_NAME##$ISCSI_TARGET_PREFIX}
    SNAPSHOT_FILENAME="$(dirname $REF_VM_DISK)/$SNAPSHOT_BASENAME"
    ISCSI_BASENAME="${SNAPSHOT_BASENAME}-iscsi"

    targetcli /iscsi/ delete "$ISCSI_TARGET_NAME" || true
    targetcli /backstores/block delete "$ISCSI_BASENAME" || true
    kpartx -s -v -d "$SNAPSHOT_FILENAME" || true
    dmsetup remove "$ISCSI_BASENAME" || true
    lvremove -f "$SNAPSHOT_FILENAME"
done
