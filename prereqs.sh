#!/bin/bash

packages=(
  nginx
  kpartx
  iscsitarget
  iscsitarget-dkms
)
apt-get -y install "${packages[@]}"
