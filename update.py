#!/usr/bin/env python3

import abc
import argparse
import contextlib
from dataclasses import dataclass
import datetime
import glob
import json
import logging
import os
import shutil
import socket
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


def reboot(host):
    logging.info('Rebooting %s', host)
    ssh(host, 'reboot')


def is_accessible(host):
    logging.info('Checking if %s is accessible', host)
    return ssh(host, 'id', options=('-o', 'ConnectTimeout=1'),
               stdout=subprocess.PIPE) == 0


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


def snapshot_glob(origin):
    return f'{origin}-at-*'


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


def get_disk(vmm, vm):
    disks = list(vmm.get_disks(vm))
    if len(disks) != 1:
        raise RuntimeError('Need exactly one disk for vm, got {disks}')
    return disks[0]


@contextlib.contextmanager
def vm_disk_snapshot(vmm, ref_vm, ref_host, timestamp, size):
    origin = None
    name = None
    with vm_shut_down(vmm, ref_vm, ref_host):
        ref_lv = get_disk(vmm, ref_vm)
        wait_for(lambda: not is_lv_open(ref_lv), timeout=30, step=1)
        origin = ref_lv
        name = snapshot_name(origin, timestamp)
        create_lvm_snapshot(origin, name, size)

    device = os.path.join(os.path.dirname(origin), name)
    try:
        assert os.path.exists(device)
        yield device
    except Exception:
        logging.error(f'Exception while using disk snapshot {name}, '
                      'removing snapshot')
        try:
            remove_lv(device)
        except Exception:
            logging.exception('Exception while removing LV %s', device)
        raise


class VirtualMachineManager(abc.ABC):

    def is_vm_running(self, name):
        pass

    def start(self, name):
        pass

    def reset(self, name):
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

    def reset(self, name):
        logging.warning('Resetting %s', name)
        subprocess.check_call(['virsh', 'reset', name])

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
                    logging.debug('Copying %s to %s', src, dst)
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


def snapshot_artifacts_path(output, snapshot_disk):
    return os.path.join(output, os.path.basename(snapshot_disk))


@contextlib.contextmanager
def snapshot_artifacts(output, snapshot_disk):
    path = snapshot_artifacts_path(output, snapshot_disk)
    assert not os.path.exists(path)
    logging.info('Creating snapshot artifacts directory %s', path)
    os.makedirs(path)
    try:
        yield path
    except Exception:
        logging.error('Exception while using artifacts directory %s, '
                      'clening up', path)
        shutil.rmtree(path)
        raise


def publish_kernel_images(root, artifacts):
    logging.info('Publishing kernel images to %s', artifacts)
    return tuple(
        shutil.copy2(os.path.join(root, file_), artifacts)
        for file_ in ('vmlinuz', 'initrd.img')
    )


def remove_iscsi_backstore(name):
    cmdline = ['targetcli', '/backstores/block', 'delete', name]
    logging.info('Removing iSCSI backstore: %s', cmdline)
    subprocess.check_call(cmdline)


def get_iscsi_backstore_name(device):
    return os.path.basename(device)


@contextlib.contextmanager
def create_iscsi_backstore(device):
    name = get_iscsi_backstore_name(device)
    cmdline = ['targetcli', '/backstores/block', 'create',
               f'dev={device}', f'name={name}', 'readonly=True']
    logging.info('Adding iSCSI backstore: %s', cmdline)
    subprocess.check_call(cmdline)
    try:
        yield name
    except Exception:
        try:
            remove_iscsi_backstore(name)
        except Exception:
            logging.exception(f'Failed to remove iSCSI backstore {name}')
        raise


def remove_iscsi_target(name):
    cmdline = ['targetcli', '/iscsi', 'delete', name]
    logging.info('Removing iSCSI target: %s', cmdline)
    subprocess.check_call(cmdline)


def attach_backstore_to_iscsi_target(target_name, backstore_name):
    cmdline = ['targetcli', f'/iscsi/{target_name}/tpg1/luns', 'create',
               f'/backstores/block/{backstore_name}']
    logging.info('Adding iSCSI LUN: %s', cmdline)
    subprocess.check_call(cmdline)


def get_iscsi_target_name(backstore_name):
    return f'iqn.2013-07.cow.{backstore_name}'


@contextlib.contextmanager
def create_iscsi_target(backstore_name):
    target_name = get_iscsi_target_name(backstore_name)
    cmdline = ['targetcli', '/iscsi', 'create', target_name]
    logging.info('Adding iSCSI target: %s', cmdline)
    subprocess.check_call(cmdline)

    try:
        attach_backstore_to_iscsi_target(target_name, backstore_name)
        yield target_name
    except Exception:
        try:
            remove_iscsi_target(target_name)
        except Exception:
            logging.exception(f'Failed to remove iSCSI target {target_name}')
        raise


def configure_authentication(target_name):
    cmdline = ['targetcli', f'/iscsi/{target_name}/tpg1', 'set', 'attribute',
               'generate_node_acls=1']
    logging.info('Configuring iSCSI authentication: %s', cmdline)
    subprocess.check_call(cmdline)


def save_iscsi_config():
    cmdline = ['targetcli', 'saveconfig']
    logging.debug('Saving iSCSI configuration: %s', cmdline)
    subprocess.check_call(cmdline)


@contextlib.contextmanager
def publish_to_iscsi(device):
    try:
        with contextlib.ExitStack() as stack:
            backstore_name = stack.enter_context(
                create_iscsi_backstore(device)
            )
            target_name = stack.enter_context(
                create_iscsi_target(backstore_name)
            )
            configure_authentication(target_name)
            save_iscsi_config()
            yield target_name
    except Exception:
        try:
            save_iscsi_config()
        except Exception:
            logging.exception('Failed to save iSCSI config')
        raise


def ipxe_config_filename(output, iscsi_target_name):
    return os.path.join(output, f'{iscsi_target_name}.ipxe')


@contextlib.contextmanager
def generate_ipxe_config(output, iscsi_target_name, kernel, initrd):
    kernel_path = os.path.relpath(kernel, output)
    initrd_path = os.path.relpath(initrd, output)
    config_path = ipxe_config_filename(output, iscsi_target_name)
    with open(config_path, 'w') as config_output:
        config_output.write(f'''#!ipxe

set iti {socket.getfqdn()}
set itn {iscsi_target_name}
set iscsi_params iscsi_target_ip=${{iti}} iscsi_target_name=${{itn}}
set cow_params cowsrc=network cowtype=${{cowtype}} root=/dev/mapper/root
set params ${{iscsi_params}} ${{cow_params}}

kernel {kernel_path} BOOTIF=01-${{netX/mac}} ${{params}} quiet
initrd {initrd_path}
boot
''')
    try:
        yield config_path
    except Exception:
        os.unlink(config_path)
        raise


@contextlib.contextmanager
def saved_config(path):
    old_path = f'{path}.old'
    if os.path.exists(old_path):
        logging.warning('Old config %s exists, removing', old_path)
        os.unlink(old_path)

    if not os.path.exists(path):
        logging.warning('%s does not exist', path)
    else:
        os.rename(path, old_path)

    try:
        yield old_path
    except Exception:
        logging.warning('Restoring config %s from %s', path, old_path)
        if os.path.exists(old_path):
            os.rename(old_path, path)
        raise
    else:
        os.unlink(old_path)


@contextlib.contextmanager
def published_ipxe_config(output, config, testing=False):
    path = os.path.join(output, 'boot-test.ipxe' if testing else 'boot.ipxe')
    logging.info(f'Publishing{" testing" if testing else ""} iPXE config '
                 'to %s', path)
    with saved_config(path):
        os.symlink(config, path)
        try:
            yield path
        except Exception:
            os.unlink(path)
            raise


@contextlib.contextmanager
def reset_back_on_failure(vmm, vm):
    try:
        yield
    except Exception:
        try:
            vmm.reset(vm)
        except Exception:
            logging.exception('Failed to reset VM %s', vm)
        raise


def reboot_and_check_test_vm(vmm, vm, host, timestamp):
    def booted_properly(host):
        if not is_accessible(host):
            return False
        cmdline = ['ssh', host, 'cat', '/etc/timestamp']
        logging.debug('Running %s', cmdline)
        try:
            output = subprocess.check_output(cmdline, text=True).strip()
            if output != timestamp:
                logging.warning('Actual timestamp %s is not expected %s',
                                output, timestamp)
            return True
        except Exception:
            logging.exception('Failed to get timestamp from %s', host)

    if is_accessible(host):
        reboot(host)
    else:
        logging.warning('%s is not accessble', host)
        vmm.reset(vm)

    wait_for(lambda: booted_properly(host), timeout=180, step=10)


def parse_partitions_config(value):
    with open(value) as config_input:
        return PartitionsConfig(**json.load(config_input))


def parse_args(raw_args):
    parser = argparse.ArgumentParser(raw_args[0])
    parser.add_argument('-v', '--verbose', action='count', default=0)
    subparsers = parser.add_subparsers(help='subcommand to execute')

    add_parser = subparsers.add_parser('add', help='Add new snapshot')
    add_parser.add_argument('-s', '--snapshot-size', default='5G')
    add_parser.add_argument('--to-copy', action='append')
    add_parser.add_argument('--chroot-script')
    add_parser.add_argument('ref_vm')
    add_parser.add_argument('ref_host')
    add_parser.add_argument('partitions_config', type=parse_partitions_config)
    add_parser.add_argument('output')
    add_parser.add_argument('test_vm')
    add_parser.add_argument('test_host')
    add_parser.set_defaults(func=add_snapshot)

    clean_parser = subparsers.add_parser('clean', help='Cleanup old snapshots')
    clean_parser.add_argument('--force-old', action='store_true')
    clean_parser.add_argument('--force-latest', action='store_true')
    clean_parser.add_argument('ref_vm')
    clean_parser.add_argument('output')
    clean_parser.set_defaults(func=clean_snapshots)

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


def add_snapshot(args):
    vmm = Virsh()
    check_preconditions(vmm, args.ref_vm, args.ref_host)

    timestamp = generate_timestamp()
    with contextlib.ExitStack() as snapshot_stack:
        snapshot_disk = snapshot_stack.enter_context(vm_disk_snapshot(
            vmm, args.ref_vm, args.ref_host, timestamp, args.snapshot_size
        ))
        artifacts = snapshot_stack.enter_context(
            snapshot_artifacts(args.output, snapshot_disk)
        )
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
        with contextlib.ExitStack() as fs_stack:
            fs_stack.enter_context(partitions_exposed(snapshot_disk))
            root = fs_stack.enter_context(chroot(net_partition.kpartx_name))
            copy_files(root, args.to_copy)
            write_timestamp(root, timestamp)
            write_cow_config(args, root)
            run_chroot_script(root, args.chroot_script)
            kernel, initrd = publish_kernel_images(root, artifacts)

        iscsi_target_name = snapshot_stack.enter_context(
            publish_to_iscsi(snapshot_disk)
        )
        ipxe_config = snapshot_stack.enter_context(generate_ipxe_config(
            args.output, iscsi_target_name, kernel, initrd
        ))

        snapshot_stack.enter_context(reset_back_on_failure(vmm, args.test_vm))
        snapshot_stack.enter_context(published_ipxe_config(
            args.output, ipxe_config, testing=True
        ))
        reboot_and_check_test_vm(vmm, args.test_vm, args.test_host, timestamp)
        with published_ipxe_config(args.output, ipxe_config):
            logging.info('Finished')


def get_snapshots(vmm, vm):
    pattern = snapshot_glob(get_disk(vmm, vm))
    return sorted(glob.glob(pattern))


def get_dynamic_iscsi_sessions(target_name):
    dynamic_sessions_file = os.path.join(
        '/sys/kernel/config/target/iscsi',
        target_name, 'tpgt_1/dynamic_sessions'
    )
    if not os.path.exists(dynamic_sessions_file):
        return []

    with open(dynamic_sessions_file) as sessions_input:
        return list(map(str.strip, sessions_input.read().splitlines()))


def clean_snapshot(output, name, force=False):
    backstore_name = get_iscsi_backstore_name(name)
    target_name = get_iscsi_target_name(backstore_name)
    sessions = get_dynamic_iscsi_sessions(target_name)
    if sessions:
        logging.warning('Snapshot %s has the following dynamic sessions:',
                        name)
        for session in sessions:
            logging.warning('  %s', session)
        if not force:
            logging.warning('Skipping cleanup')
            return
        else:
            logging.warning('Continuing as requested')

    ipxe_config = ipxe_config_filename(output, target_name)
    if os.path.exists(ipxe_config):
        logging.info('Cleaning iPXE config at %s', ipxe_config)
        os.unlink(ipxe_config)

    artifacts = snapshot_artifacts_path(output, name)
    if os.path.exists(artifacts):
        logging.info('Cleaning snapshot artifacts at %s', artifacts)
        shutil.rmtree(artifacts)

    try:
        remove_iscsi_target(target_name)
    except Exception:
        logging.warning(f'Failed to remove iSCSI target {target_name}')

    try:
        remove_iscsi_backstore(backstore_name)
    except Exception:
        logging.warning(f'Failed to remove iSCSI backstore {backstore_name}')

    cleanup_kpartx(name)
    if is_lv_open(name):
        raise RuntimeError(f'LV {name} is still open')

    logging.info('LV %s is not open, proceeding with remove', name)
    remove_lv(name)


def clean_snapshots(args):
    vmm = Virsh()
    snapshots = get_snapshots(vmm, args.ref_vm)
    if not snapshots:
        return

    old, latest = snapshots[:-1], snapshots[-1]

    for snapshot in old:
        clean_snapshot(args.output, snapshot, force=args.force_old)

    if args.force_latest:
        logging.warning('Removing latest snapshot %s', latest)
        clean_snapshot(args.output, latest, force=True)


def main(raw_args):
    args = parse_args(raw_args)
    configure_logging(args)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main(sys.argv))