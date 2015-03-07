#!/bin/bash

packages=(open-iscsi kpartx puppet dpkg-dev mdadm augeas-tools watchdog)

apt-get -y install "${packages[@]}"

/etc/init.d/mdadm stop
