#!/bin/bash

packages=(
  kpartx
  iscsitarget
  iscsitarget-dkms
)
apt-get -y install "${packages[@]}"
