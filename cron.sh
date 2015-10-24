#!/usr/bin/env bash

BASE=$(dirname "$0")
CONFIG="$BASE/conf/$(hostname -f).sh"

locked() {
  flock -n -E 10 "$BASE" "$@"
}

silent() {
  local allfile="$(tempfile -p cow)"
  "$@" >"$allfile" 2>&1; rv="$?"
  [[ "$rv" -ne 0 ]] && cat "$allfile"
  rm "$allfile"
  return "$rv"
}

if [[ ! -f "$CONFIG" ]]; then
  echo "no config file: $CONFIG"
  exit 1
fi

case "$1" in
  new)
    silent locked "$BASE/update.sh" "$CONFIG"
  ;;
  cleanup)
    silent locked "$BASE/iet.py" "$CONFIG"
  ;;
  *)
    echo "usage: $0 {new,cleanup}"
    exit 1
  ;;
esac
