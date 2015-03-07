#!/usr/bin/env bash

[[ "$(cat /sys/class/iscsi_session/*/state)" = 'LOGGED_IN' ]] &&
    [[ "$(dmesg | grep 'detected conn error' | wc -l)" -eq 0 ]]
