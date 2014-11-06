#!/usr/bin/env bash

# disable puppet and remove it's monitoring file
sed -i 's/^START=yes$/START=no/' /etc/default/puppet
rm /var/lib/puppet/ssl/{certs,public_keys,private_keys}/*
rm -f /usr/local/puppet.random

# clear transient home
rm -rf /home/*

# clear logs
find /var/log -type f -delete

# clear host SSH keys
# TODO: replace host key with temporary key
#rm /etc/ssh/ssh_host_{r,d}sa_key{,.pub}

# add net script as a prerequisite for iscsi start in initramfs
sed -i 's/^PREREQ=""/PREREQ="net"/' \
  /usr/share/initramfs-tools/scripts/local-top/iscsi
update-initramfs -u

update-rc.d -f open-iscsi  remove
update-rc.d -f hostname.sh remove

insserv cond-mkfs.sh
sed -i 's/+mountall /+cond-mkfs +mountall /' /etc/insserv.conf
