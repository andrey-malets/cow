#!/bin/bash

packages=(open-iscsi kpartx puppet dpkg-dev)
apt-get -y install "${packages[@]}"
