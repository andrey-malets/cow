#!/usr/bin/env python3

import abc
import argparse
import contextlib
from dataclasses import dataclass
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def ssh(host, command, options=None, **kwargs):
    cmdline = ['ssh']
    if options is not None:
        cmdline.extend(options)
    cmdline.extend([host, command])
    logging.debug('Running %s', cmdline)
    return subprocess.call(cmdline, **kwargs)


class Timeout(Exception):
    pass


def wait_for(condition, timeout, step):
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition():
            return True
        time.sleep(step)

    raise Timeout(f'Failed to wait {timeout} seconds for f{condition}')


def no_dpkg_locks(host):
    return ssh(host, '! fuser /var/lib/dpkg/lock') == 0


def shutdown(host):
    logging.info('Waiting for no dpkg locks on %s', host)
    wait_for(lambda: no_dpkg_locks(host), timeout=900, step=10)
    logging.info('Shutting down %s', host)
    ssh(host, 'shutdown now')


def is_accessible(host):
    logging.info('Checking if %s is accessible', host)
    return ssh(host, 'id', options=('-o', 'ConnectTimeout=1'),
               stdout=subprocess.PIPE) == 0


def is_lv_open(name):
    logging.info('Checking if LV %s is open', name)
    cmdline = ['lvs', '-o', 'lv_attr', '--noheadings', name]
    logging.debug('Running %s', cmdline)
    output = subprocess.check_output(cmdline, text=True).strip()
    flag = output[5]
    if flag == '-':
        return False
    elif flag == 'o':
        return True
    else:
        raise RuntimeError(f'Cannot parse LV attributes "{output}"')


@dataclass(frozen=True)
class DiskConfiguration:
    path: str
    size: str
    transport: str
    logical_sector_size: int
    physical_sector_size: int
    partition_table_type: str
    model: str


@dataclass(frozen=True)
class PartitionConfiguration:
    number: int
    begin: str
    end: str
    size: str
    filesystem_type: str
    name: str
    kpartx_name: str
    flags_set: str


@dataclass(frozen=True)
class DiskInformation:
    type: str
    configuration: DiskConfiguration
    partitions: list


class DiskConfigError(Exception):

    def __init__(self, message, device, real_device, parted_output):
        super().__init__(
            f'{message} for device {device} (real device {real_device}). '
            f'Parted output was: {parted_output}'
        )


def cleanup_kpartx(device):
    cmdline = ['kpartx', '-d', '-v', device]
    for delay in (0.1, 0.3, 0.5, 1, 2, 3, None):
        logging.debug('Running %s', cmdline)
        result = subprocess.run(cmdline, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        if result.returncode == 0:
            return
        if 'is in use' in result.stdout:
            logging.warning('Some partitions of %s are still in use: ', device)
            logging.warning(result.stdout)
            if delay is not None:
                logging.info('waiting for %.01f seconds', delay)
                time.sleep(delay)
        else:
            raise RuntimeError('Unexpected error from kpartx: '
                               f'{result.stdout}')

    raise RuntimeError(f'Failed to cleanup partitions for {device} '
                       'with kpartx')


def get_kpartx_names(device):
    cmdline = ['kpartx', '-l', '-s', device]
    logging.debug('Running %s', cmdline)
    try:
        output = subprocess.check_output(cmdline, text=True)
        result = {}
        for index, line in enumerate(output.splitlines()):
            name = line.split(' ', 1)[0]
            result[int(index + 1)] = f'/dev/mapper/{name}'
        return result
    finally:
        try:
            cleanup_kpartx(device)
        except Exception:
            logging.exception('Exception while cleaning up partitions '
                              'for device %s', device)


@contextlib.contextmanager
def partitions_exposed(device):
    cmdline = ['kpartx', '-a', '-s', device]
    logging.debug('Running %s', cmdline)
    subprocess.check_call(cmdline)
    try:
        yield
    finally:
        try:
            cleanup_kpartx(device)
        except Exception:
            logging.exception('Exception while cleaning up partitions '
                              'for device %s', device)


def parse_partitions(device, lines):
    kpartx_names = get_kpartx_names(device)
    for line in lines:
        assert line.endswith(';')
        number, begin, end, size, fs, name, flags = line[:-1].split(':')
        yield PartitionConfiguration(
            number=int(number),
            begin=begin,
            end=end,
            size=size,
            filesystem_type=fs,
            name=name,
            kpartx_name=kpartx_names[int(number)],
            flags_set=flags,
        )


def get_disk_information(device):
    real_device = os.path.realpath(device)
    cmdline = ['parted', '-s', '-m', real_device, 'print']
    logging.debug('Running %s', cmdline)
    output = subprocess.check_output(cmdline, text=True)
    lines = list(line.strip() for line in output.splitlines())
    if len(lines) < 2:
        raise DiskConfigError(
            'Expected at least two lines in parted output',
            device, real_device, output
        )

    BYTES = 'BYT'
    if lines[0] != f'{BYTES};':
        raise DiskConfigError(
            'Only "Bytes" units are supported',
            device, real_device, output
        )

    path, size, transport, lss, pss, ptt, model, end = lines[1].split(':')
    if path != real_device:
        raise DiskConfigError(
            'Expected device spec as second line of parted output',
            device, real_device, output
        )

    disk_config = DiskConfiguration(
        path=path,
        size=size,
        transport=transport,
        logical_sector_size=int(lss),
        physical_sector_size=int(pss),
        partition_table_type=ptt,
        model=model,
    )

    return DiskInformation(
        type=BYTES,
        configuration=disk_config,
        partitions=list(parse_partitions(device, lines[2:])),
    )


def get_partition(device, disk_info, name):
    parts = list(part for part in disk_info.partitions if part.name == name)
    if len(parts) != 1:
        raise RuntimeError(f'Expected exactly one partition with name {name} '
                           f'on device {device}, got {disk_info.partitions}')
    return parts[0]


def set_partition_name(device, number, name):
    logging.info('Setting partition name to %s for partition number %d on %s',
                 name, number, device)
    cmdline = ['parted', '-s', device, 'name', str(number), name]
    logging.debug('Running %s', cmdline)
    subprocess.check_call(cmdline)


def generate_timestamp():
    return datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')


def snapshot_name(origin, timestamp):
    return f'{os.path.basename(origin)}-at-{timestamp}'


def create_lvm_snapshot(origin, name, size):
    cmdline = ['lvcreate', '-s', '-L', size, '-n', name, origin]
    logging.debug('Running %s', cmdline)
    subprocess.check_call(cmdline)


def remove_lv(name):
    cmdline = ['lvremove', '-f', name]
    logging.debug('Running %s', cmdline)
    subprocess.check_call(cmdline)


@contextlib.contextmanager
def mounted(device, mountpoint, type_=None, options=None):
    assert os.path.exists(mountpoint), f'{mountpoint} does not exist'

    mount_cmdline = ['mount']
    if type_ is not None:
        mount_cmdline.extend(['-t', type_])
    if options is not None:
        mount_cmdline.extend(options)
    mount_cmdline.append('none' if device is None else device)
    mount_cmdline.append(mountpoint)

    logging.debug('Running %s', mount_cmdline)
    subprocess.check_call(mount_cmdline)
    try:
        yield
    finally:
        try:
            umount_cmdline = ['umount', mountpoint]
            logging.debug('Running %s', umount_cmdline)
            subprocess.check_call(umount_cmdline)
        except Exception:
            logging.exception('Failed to unmount %s', mountpoint)


@contextlib.contextmanager
def chroot(partition):
    with contextlib.ExitStack() as stack:
        root = stack.enter_context(
            tempfile.TemporaryDirectory(prefix='snapshot_root_')
        )
        stack.enter_context(mounted(partition, root))
        stack.enter_context(mounted(None, os.path.join(root, 'proc'),
                                    type_='proc'))
        stack.enter_context(mounted(None, os.path.join(root, 'sys'),
                                    type_='sysfs'))
        stack.enter_context(mounted('/dev', os.path.join(root, 'dev'),
                                    options=('--bind',)))
        stack.enter_context(mounted('/dev/pts',
                                    os.path.join(root, 'dev', 'pts'),
                                    options=('--bind',)))
        yield root


@contextlib.contextmanager
def vm_disk_snapshot(vmm, ref_vm, ref_host, timestamp, size):
    origin = None
    name = None
    with vm_shut_down(vmm, ref_vm, ref_host):
        disks = list(vmm.get_disks(ref_vm))
        if len(disks) != 1:
            raise RuntimeError('Need exactly one disk for ref vm, got {disks}')
        ref_lv = disks[0]
        wait_for(lambda: not is_lv_open(ref_lv), timeout=30, step=1)
        origin = ref_lv
        name = snapshot_name(origin, timestamp)
        create_lvm_snapshot(origin, name, size)

    device = os.path.join(os.path.dirname(origin), name)
    try:
        assert os.path.exists(device)
        yield device
    except Exception:
        logging.exception(f'Exception while using disk snapshot {name}, '
                          'removing snapshot')
        try:
            remove_lv(device)
        except Exception:
            logging.exception('Exception while removing LV %s', device)


class VirtualMachineManager(abc.ABC):

    def is_vm_running(self, name):
        pass

    def start(self, name):
        pass

    def get_disks(self, name):
        pass


class Virsh(VirtualMachineManager):

    def is_vm_running(self, name):
        logging.info('Checking if %s is running', name)
        cmdline = ['virsh', 'list', '--state-running', '--name']
        logging.debug('Running %s', cmdline)
        list_output = subprocess.check_output(cmdline, text=True)
        domains = set(d.strip() for d in list_output.splitlines() if d)
        logging.info('Running domains: %s', domains)

        return name in domains

    def start(self, name):
        logging.info('Starting %s', name)
        subprocess.check_call(['virsh', 'start', name])

    def get_disks(self, name):
        cmdline = ['virsh', 'dumpxml', name]
        logging.debug('Running %s', cmdline)
        xml = subprocess.check_output(cmdline, text=True)
        root = ET.fromstring(xml)
        for disk in root.findall('./devices/disk/source'):
            yield disk.get('dev')


@contextlib.contextmanager
def vm_shut_down(vmm, name, host):
    shutdown(host)
    wait_for(lambda: not vmm.is_vm_running(name), timeout=180, step=3)
    try:
        yield
    finally:
        vmm.start(name)
        try:
            wait_for(lambda: is_accessible(host), 300, 5)
        except Timeout:
            logging.exception('Timed out waiting for %s to become accessbile '
                              'with ssh', host)


@dataclass(frozen=True)
class PartitionsConfig:
    base: str
    network: str
    local: str
    cow: str
    conf: str
    sign: str
    keyimage: str
    place: str


def parse_partitions_config(value):
    with open(value) as config_input:
        return PartitionsConfig(**json.load(config_input))


def parse_args(raw_args):
    parser = argparse.ArgumentParser(raw_args[0])
    parser.add_argument('-v', '--verbose', action='count', default=0)
    parser.add_argument('-s', '--snapshot-size', default='5G')
    parser.add_argument('--to-copy', action='append')
    parser.add_argument('--chroot-script')
    parser.add_argument('ref_vm')
    parser.add_argument('ref_host')
    parser.add_argument('partitions_config', type=parse_partitions_config)
    parser.add_argument('output')

    return parser.parse_args(raw_args[1:])


def configure_logging(args):
    levels = {
        0: logging.WARN,
        1: logging.INFO,
    }
    logging.basicConfig(
        level=levels.get(args.verbose, logging.DEBUG),
        format='%(asctime)s: %(levelname)-8s ' '%(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def check_preconditions(vmm, ref_vm, ref_host):
    if not vmm.is_vm_running(ref_vm):
        raise RuntimeError(f'Reference vm {ref_vm} is not running')

    if not is_accessible(ref_host):
        raise RuntimeError(f'Reference host {ref_host} is not accessible '
                           'with ssh')


def copy_files(root, to_copy):
    def relpath(top, dirpath, path):
        return os.path.relpath(os.path.join(dirpath, path), top)

    for dir_ in to_copy:
        logging.info('Copying contents of %s to %s', dir_, root)
        assert os.path.isdir(dir_)
        for dirpath, dirnames, filenames in os.walk(dir_):
            for dirname in dirnames:
                dst = os.path.join(root, relpath(dir_, dirpath, dirname))
                os.makedirs(dst, exist_ok=True)
            for filename in filenames:
                src = os.path.join(dirpath, filename)
                dst = os.path.join(root, relpath(dir_, dirpath, filename))
                if os.path.exists(dst):
                    logging.debug('Overwriting %s with %s', dst, src)
                else:
                    logging.debug('Copying %s with %s', src, dst)
                shutil.copy2(src, dst)


def write_timestamp(root, timestamp):
    with open(os.path.join(root, 'etc', 'timestamp'), 'w') as timestamp_out:
        print(timestamp, file=timestamp_out)


def write_cow_config(args, root):
    config_path = os.path.join(root, 'etc', 'cow.conf')
    logging.info('Writing cow config to %s', config_path)
    with open(config_path, 'w') as config_output:
        PARTITION_NAMES = 'PARTITION_NAMES'
        config_output.write(f'declare -A {PARTITION_NAMES}\n')
        for key, value in vars(args.partitions_config).items():
            config_output.write(f'{PARTITION_NAMES}[{key}]={value}\n')


def run_chroot_script(root, script):
    if script is not None:
        logging.info('Running chroot script %s in %s', script, root)
        cmdline = ['chroot', root, script]
        logging.debug('Running %s', cmdline)
        subprocess.check_call(cmdline)


def publish_kernel_images(root, output):
    logging.info('Publishing kernel images to %s', output)
    for file_ in ('vmlinuz', 'initrd.img'):
        shutil.copy(os.path.join(root, file_), output)


def main(raw_args):
    args = parse_args(raw_args)
    configure_logging(args)

    vmm = Virsh()
    check_preconditions(vmm, args.ref_vm, args.ref_host)

    timestamp = generate_timestamp()
    with contextlib.ExitStack() as stack:
        snapshot_disk = stack.enter_context(vm_disk_snapshot(
            vmm, args.ref_vm, args.ref_host, timestamp, args.snapshot_size
        ))
        logging.info('Snapshot disk is %s', snapshot_disk)
        disk_info = get_disk_information(snapshot_disk)
        assert disk_info.configuration.partition_table_type == 'gpt', (
            'VMs must have disk with GPT partitoin table'
        )
        base_partition = get_partition(snapshot_disk, disk_info,
                                       args.partitions_config.base)
        set_partition_name(snapshot_disk, base_partition.number,
                           args.partitions_config.network)
        disk_info = get_disk_information(snapshot_disk)
        net_partition = get_partition(snapshot_disk, disk_info,
                                      args.partitions_config.network)
        stack.enter_context(partitions_exposed(snapshot_disk))

        root = stack.enter_context(chroot(net_partition.kpartx_name))
        copy_files(root, args.to_copy)
        write_timestamp(root, timestamp)
        write_cow_config(args, root)
        run_chroot_script(root, args.chroot_script)
        publish_kernel_images(root, args.output)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
