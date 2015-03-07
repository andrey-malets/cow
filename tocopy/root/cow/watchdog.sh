#!/usr/bin/env bash

[[ "$(cat /sys/class/iscsi_session/*/state)" = 'LOGGED_IN' ]]
