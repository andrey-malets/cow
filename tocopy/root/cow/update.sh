#!/usr/bin/env bash

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

# add net script as a prerequisite for iscsi start in initramfs
sed -i 's/^PREREQ=""/PREREQ="net"/' \
  /usr/share/initramfs-tools/scripts/local-top/iscsi
update-initramfs -u

update-rc.d -f open-iscsi remove

rm /etc/hostname

insserv cond-mkfs.sh
sed -i 's/+mountall /+cond-mkfs +mountall /' /etc/insserv.conf
