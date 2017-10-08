#!/usr/bin/env bash

BASE=$(dirname "$0")

locked() {
  lockfile=$1; shift
  flock -n -E 10 -o "$lockfile" "$@"; rv="$?"
  if [[ "$rv" -eq 10 ]]; then
    echo "$lockfile is locked now, exiting"
  fi
  return "$rv"
}

silent() {
  local allfile="$(tempfile -p cow)"
  "$@" >"$allfile" 2>&1; rv="$?"
  [[ "$rv" -ne 0 ]] && cat "$allfile"
  rm "$allfile"
  return "$rv"
}

usage() {
  echo "usage: $1 {new,cleanup} <host config> <image config>" >&2
  exit 1
}

if [[ "$#" -ne 3 ]]; then usage "$0"; fi

ACTION=$1
HOST_CONFIG=$2
IMAGE_CONFIG=$3

if [[ ! -f "$HOST_CONFIG" ]] || [[ ! -f "$IMAGE_CONFIG" ]]; then usage "$0"; fi

case "$ACTION" in
  new)
    silent locked "$HOST_CONFIG" \
        "$BASE/update.sh" "$HOST_CONFIG" "$IMAGE_CONFIG"
  ;;
  cleanup)
    silent locked "$HOST_CONFIG" \
        "$BASE/cleanup.sh" "$HOST_CONFIG" "$IMAGE_CONFIG"
  ;;
  *)
    usage "$0"
  ;;
esac
