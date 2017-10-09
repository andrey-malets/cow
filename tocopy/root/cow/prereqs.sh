#!/bin/bash -xe

packages=(open-iscsi kpartx puppet dpkg-dev mdadm augeas-tools watchdog
          keyutils initramfs-tools)

PATH="$(dirname "$0")/fake:$PATH" apt-get -y install "${packages[@]}"
