# Path to the reference VM configuration file for Xen.
REF_VM_PATH=/root/xen/image64.cfg

# Path to the test VM config file for Xen.
TEST_VM_PATH=/root/xen/image64-test.cfg

# Host name of test VM, used for graceful reboot, if possible.
TEST_HOST=image64-test.urgu.org

# Size of snapshot volume used for hosting over iSCSI.
# Format may be anything accepted by lvcreate.
SNAPSHOT_SIZE=5G

# Suffix which will be appended to timestamps and reference
# VM disk snapshots to differ it from another disks. Do not
# mix with test vm name!
TIMESTAMP_SUFFIX=at-

# The names of partitions system will use for creating COW image
# and booting it.
declare -A PARTITION_NAMES

# The name of partition on the base image which will be used to
# create COW image.
PARTITION_NAMES[base]=image64-base

# The name of partition which must be present in the system
# (usually mounted over iSCSI) when network boot is selected
PARTITION_NAMES[network]=cow-image64-net

# The name of partition which must be present (presumably on
# one of local disks) when local boot is done
PARTITION_NAMES[local]=cow-image64-local

# The name of special partition for copy-on-write device.
# This should reside on the local disk and be unique for each
# machine.
PARTITION_NAMES[cow]=cow-image64-cow

# This is a name for config partition and it's signature used to
# verify config partition integrity. Should be present at the same
# disk as cow partition.
PARTITION_NAMES[conf]=cow-image64-conf
PARTITION_NAMES[sign]=cow-image64-sign

# The name of partition which will be formatted automatically and
# mounted to /place directory.
PARTITION_NAMES[place]=cow-image64-place
