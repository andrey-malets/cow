#!/usr/bin/env bash

set -x -e

# disable puppet and remove it's monitoring file
puppet agent --disable
rm /var/lib/puppet/ssl/{certs,public_keys,private_keys}/*
rm -f /usr/local/puppet.random

# clear transient home
rm -rf /home/*

# clear logs
find /var/log -type f -delete

# clear MDADM array information
echo -n > /etc/mdadm/mdadm.conf

# clear udev net and net generator rules
rm -f /etc/udev/rules.d/*net.rules \
      /lib/udev/rules.d/*net-generator.rules

# tell MDADM to assemble all of its arrays in initramfs
augtool -s set /files/etc/default/mdadm/INITRDSTART all

# clear host SSH keys
# TODO: replace host key with temporary key
#rm /etc/ssh/ssh_host_{r,d}sa_key{,.pub}

update-initramfs -u

update-rc.d -f open-iscsi remove

systemctl enable watchdog.service
systemctl enable mount-place.service

rm -f /etc/{host,mail}name
