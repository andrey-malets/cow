#!/bin/bash

packages=(open-iscsi kpartx puppet dpkg-dev mdadm augeas-tools watchdog)

PATH="$(dirname "$0")/fake:$PATH" apt-get -y install "${packages[@]}"

/etc/init.d/mdadm stop
