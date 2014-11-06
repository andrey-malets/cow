#!/bin/bash

packages=(open-iscsi kpartx puppet)
apt-get -y install "${packages[@]}"
