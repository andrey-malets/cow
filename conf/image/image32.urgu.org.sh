# Path to the reference VM configuration file for Xen.
REF_VM_PATH=/root/xen/image32.cfg

# Path to the test VM config file for Xen.
TEST_VM_PATH=/root/xen/image32-test.cfg

# Host name of test VM, used for graceful reboot, if possible.
TEST_HOST=image32-test.urgu.org

# Size of snapshot volume used for hosting over iSCSI.
# Format may be anything accepted by lvcreate.
SNAPSHOT_SIZE=5G

# Suffix which will be appended to timestamps and reference
# VM disk snapshots to differ it from another disks. Do not
# mix with test vm name!
TIMESTAMP_SUFFIX=at-
