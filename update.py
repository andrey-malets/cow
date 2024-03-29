#!/usr/bin/env python3

import abc
import argparse
import contextlib
from dataclasses import dataclass
import datetime
import errno
import fcntl
import glob
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def log_and_call(cmdline, method=subprocess.check_call, **kwargs):
    logging.debug('Running %s', cmdline)
    return method(cmdline, **kwargs)


def log_and_output(cmdline, **kwargs):
    return log_and_call(cmdline, method=subprocess.check_output, text=True,
                        **kwargs)


def ssh(host, command, output=False, options=None, **kwargs):
    cmdline = ['ssh']
    if options is not None:
        cmdline.extend(options)
    cmdline.extend([host, command])
    if output:
        return log_and_output(cmdline, **kwargs)
    else:
        return log_and_call(cmdline, method=subprocess.call, **kwargs)


class Timeout(Exception):
    pass


def wait_for(condition, timeout, step):
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition():
            return True
        time.sleep(step)

    raise Timeout(f'Failed to wait {timeout} seconds for {condition}')


@contextlib.contextmanager
def transact(prepare=None, final=None, commit=None, rollback=None):
    assert final is None or (commit is None and rollback is None), (
        'final action must only be present with no commit and rollback'
    )
    rv = None
    if prepare is not None:
        prepare_msg, prepare_fn = prepare
        if prepare_msg is not None:
            logging.info(prepare_msg)
        rv = prepare_fn()
    try:
        yield rv
    except BaseException as e:
        if any((final, rollback)):
            rollback_msg, rollback_fn = next(filter(None, (final, rollback)))
            if rollback_msg:
                logging.warning(rollback_msg)
            try:
                rollback_fn((rv, e))
            except Exception:
                logging.exception('Exception while %s', rollback_msg)
        raise
    else:
        if any((final, commit)):
            commit_msg, commit_fn = next(filter(None, (final, commit)))
            if commit_msg:
                logging.info(commit_msg)
            try:
                commit_fn((rv, None))
            except Exception:
                logging.exception('Exception while %s', commit_msg)


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
        result = log_and_call(cmdline, method=subprocess.run, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
        output = log_and_output(cmdline)
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
    with transact(
        prepare=(
            f'Exposing kpartx partitions for {device}',
            lambda: log_and_call(['kpartx', '-a', '-s', device])
        ),
        final=(
            f'cleaning up partitions for device {device}',
            lambda _: cleanup_kpartx(device)
        )
    ):
        yield


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
    output = log_and_output(['parted', '-s', '-m', real_device, 'print'])
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
    log_and_call(['parted', '-s', device, 'name', str(number), name])


def generate_timestamp():
    return datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')


def non_volatile_pv(cache_config):
    return (cache_config.non_volatile_pv if cache_config else None)


LVM_SNAPSHOT_SUFFIX = '-snapshot'


def lvm_snapshot_name(origin, timestamp):
    return f'{os.path.basename(origin)}-at-{timestamp}'


def vm_snapshot_name(lvm_snapshot_name):
    return f'{lvm_snapshot_name}-snapshot'


def snapshot_copy_name(vm_snapshot_name):
    return f'{vm_snapshot_name}-copy'


def lv_path(vg, lv):
    return f'/dev/{vg}/{lv}'


def cache_lv_name(vm_snapshot_name):
    return f'{vm_snapshot_name}-cache'


def snapshot_glob(origin):
    return f'{origin}-at-*-snapshot'


def is_lv_open(name):
    logging.info('Checking if LV %s is open', name)
    cmdline = ['lvs', '-o', 'lv_attr', '--noheadings', name]
    output = log_and_output(cmdline).strip()
    flag = output[5]
    if flag == '-':
        return False
    elif flag == 'o':
        return True
    else:
        raise RuntimeError(f'Cannot parse LV attributes "{output}"')


def create_lvm_snapshot(origin, name, non_volatile_pv, size=None,
                        extents=None):
    cmdline = ['lvcreate', '-y', '-s', '-n', name]
    if size:
        cmdline.extend(('-L', size))
    else:
        assert extents
        cmdline.extend(('-l', extents))
    cmdline.append(origin)
    if non_volatile_pv is not None:
        cmdline.append(non_volatile_pv)
    log_and_call(cmdline)


def remove_lv(name):
    log_and_call(['lvremove', '-f', name])


def umount(mountpoint):
    log_and_call(['umount', mountpoint])


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

    logging.info('Mounting %s to %s', device, mountpoint)
    log_and_call(mount_cmdline)

    with transact(final=(f'unmouning {mountpoint}',
                         lambda _: log_and_call(['umount', mountpoint]))):
        yield


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


def create_vm_disk_snapshot(vmm, vm, host, timestamp, size, non_volatile_pv):
    origin = None
    name = None
    with vm_shut_down(vmm, vm, host):
        lv = get_disk(vmm, vm)
        wait_for(lambda: not is_lv_open(lv), timeout=30, step=1)
        origin = lv
        name = lvm_snapshot_name(origin, timestamp)
        create_lvm_snapshot(origin, name, non_volatile_pv, size=size)

    return os.path.join(os.path.dirname(origin), name)


def create_lvm_volume(name, size, vg, pv=None):
    create_cmdline = ['lvcreate', '-y', '-L', f'{size}B', '-n', name, vg]
    if pv is not None:
        create_cmdline.append(pv)
    log_and_call(create_cmdline)
    return name


def create_volume_copy(src, dst, non_volatile_pv):
    size = log_and_output(['blockdev', '--getsize64', src]).strip()
    vg = os.path.basename(os.path.dirname(src))
    return os.path.join(
        os.path.dirname(src),
        create_lvm_volume(dst, size, vg, non_volatile_pv)
    )


@contextlib.contextmanager
def volume_copy(src, dst, non_volatile_pv):
    with transact(
        prepare=(
            f'copying LVM {src} to {dst}',
            lambda: create_volume_copy(src, dst, non_volatile_pv)
        ),
        rollback=(
            'cleaning up LVM copy',
            lambda result: remove_lv(result[0])
        )
    ) as copy_name:
        yield copy_name


def copy_data(src, dst, block_size='128M'):
    logging.info('Copying data from %s to %s', src, dst)
    log_and_call(['dd', f'if={src}', f'of={dst}', f'bs={block_size}'])


def move_link(src, dst):
    new_dst = f'{dst}.new'
    if os.path.exists(new_dst):
        logging.waring('%s already exists, removing', new_dst)
        os.unlink(new_dst)
    os.symlink(src, new_dst)
    os.rename(new_dst, dst)


@contextlib.contextmanager
def link_snapshot_copy(origin, copy_to, non_volatile_pv):
    copy_name = snapshot_copy_name(origin)
    with contextlib.ExitStack() as stack:
        copy = stack.enter_context(
            volume_copy(origin, copy_name, non_volatile_pv)
        )
        copy_data(origin, copy)
        stack.enter_context(transact(
            commit=(
                f'linking snapshot copy {copy_name} to {copy_to}',
                lambda _: move_link(copy, copy_to)
            ),
        ))
        yield


def create_cache_volume(non_cached_name, config):
    name = cache_lv_name(non_cached_name)
    logging.info('Adding cache volume %s for %s', non_cached_name, name)
    return create_lvm_volume(name, config.cache_volume_size,
                             config.volume_group, config.cache_pv)


@contextlib.contextmanager
def cache_volume(non_cached_name, config):
    with transact(
        prepare=(
            None,
            lambda: create_cache_volume(non_cached_name, config)
        ),
        rollback=(
            f'removing cache volume for {non_cached_name}',
            lambda result: remove_lv(result[0])
        )
    ) as cached_name:
        yield cached_name


def cache_record_file(config, volume):
    return os.path.join(config.cached_volumes_path, os.path.basename(volume))


def create_cache_record(config, volume):
    record_file = cache_record_file(config, volume)
    os.makedirs(os.path.dirname(record_file), exist_ok=True)
    with open(record_file, 'w'):
        pass


def delete_cache_record(config, volume):
    record_file = cache_record_file(config, volume)
    try:
        os.remove(record_file)
    except FileNotFoundError:
        logging.warning('Cache record file %s does not exist', record_file)


def list_cache_records(config):
    return os.listdir(config.cached_volumes_path)


@contextlib.contextmanager
def cache_record(name, config):
    with transact(
        prepare=(
            f'Adding cache record for {name}',
            lambda: create_cache_record(config, name)
        ),
        rollback=(
            f'Deleting cache record for {name}',
            lambda _: delete_cache_record(config, name)
        )
    ):
        yield


def configure_caching(non_cached_volume, config):
    if config is None:
        logging.info('Caching is not configured, skipping cache for %s',
                     non_cached_volume)
        return non_cached_volume
    try:
        with contextlib.ExitStack() as stack:
            cache_volume_name = stack.enter_context(
                cache_volume(non_cached_volume, config)
            )
            stack.enter_context(cache_record(non_cached_volume, config))
            enable_cmdline = [
                'lvconvert', '-y', '--type', 'cache',
                '--cachevol', cache_volume_name,
                '--cachemode', 'writethrough', non_cached_volume
            ]
            logging.info('Enabling cache for %s on %s', non_cached_volume,
                         cache_volume_name)
            log_and_call(enable_cmdline)
            cached_volume = non_cached_volume
            return cached_volume
    except Exception:
        logging.exception('Failed to enable caching for %s', non_cached_volume)
        return non_cached_volume


@contextlib.contextmanager
def vm_disk_snapshot(vmm, ref_vm, ref_host, timestamp, size, cache_config):
    nvpv = non_volatile_pv(cache_config)
    with contextlib.ExitStack() as stack:
        with transact(
            prepare=(
                f'Creating disk snapshot of {ref_vm}',
                lambda: create_vm_disk_snapshot(vmm, ref_vm, ref_host,
                                                timestamp, size, nvpv)
            ),
            final=(
                'cleaning up disk snapshot',
                lambda result: remove_lv(result[0])
            )
        ) as lvm_snapshot:
            assert os.path.exists(lvm_snapshot)
            vm_snapshot = stack.enter_context(volume_copy(
                lvm_snapshot, vm_snapshot_name(os.path.basename(lvm_snapshot)),
                nvpv
            ))
            assert os.path.exists(vm_snapshot)
            copy_data(lvm_snapshot, vm_snapshot)
        yield vm_snapshot


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
        list_output = log_and_output(cmdline)
        domains = set(d.strip() for d in list_output.splitlines() if d)
        logging.info('Running domains: %s', domains)

        return name in domains

    def start(self, name):
        log_and_call(['virsh', 'start', name])

    def reset(self, name):
        logging.warning('Resetting %s', name)
        log_and_call(['virsh', 'reset', name])

    def get_disks(self, name):
        xml = log_and_output(['virsh', 'dumpxml', name])
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
            raise


@dataclass(frozen=True)
class CowPartitionsConfig:
    base: str
    network: str
    local: str
    cow: str
    conf: str
    sign: str
    keyimage: str
    place: str


@dataclass(frozen=True)
class CacheConfig:
    volume_group: str
    non_volatile_pv: str
    cache_pv: str
    cache_volume_size: str
    cached_volumes_path: str


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
        log_and_call(['chroot', root, script])


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
    logging.info('Removing iSCSI backstore %s', name)
    log_and_call(['targetcli', '/backstores/block', 'delete', name])


def get_iscsi_backstore_name(device):
    return os.path.basename(device)


@contextlib.contextmanager
def create_iscsi_backstore(device):
    name = get_iscsi_backstore_name(device)
    cmdline = ['targetcli', '/backstores/block', 'create',
               f'dev={device}', f'name={name}', 'readonly=True']
    logging.info('Adding iSCSI backstore %s', name)
    log_and_call(cmdline)
    with transact(
        rollback=(
            f'cleaning up iSCSI backstore {name}',
            lambda _: remove_iscsi_backstore(name)
        )
    ):
        yield name


def remove_iscsi_target(name):
    logging.info('Removing iSCSI target %s', name)
    log_and_call(['targetcli', '/iscsi', 'delete', name])


def attach_backstore_to_iscsi_target(target_name, backstore_name):
    logging.info('Adding iSCSI LUN to %s from %s', target_name, backstore_name)
    cmdline = ['targetcli', f'/iscsi/{target_name}/tpg1/luns', 'create',
               f'/backstores/block/{backstore_name}']
    log_and_call(cmdline)


def get_iscsi_target_name(backstore_name):
    return f'iqn.2013-07.cow.{backstore_name}'


@contextlib.contextmanager
def create_iscsi_target(backstore_name):
    target_name = get_iscsi_target_name(backstore_name)
    logging.info('Adding iSCSI target %s', target_name)
    log_and_call(['targetcli', '/iscsi', 'create', target_name])

    with transact(
        rollback=(
            f'cleaning up iSCSI target {target_name}',
            lambda _: remove_iscsi_target(target_name)
        )
    ):
        attach_backstore_to_iscsi_target(target_name, backstore_name)
        yield target_name


def configure_authentication(target_name):
    cmdline = ['targetcli', f'/iscsi/{target_name}/tpg1', 'set', 'attribute',
               'generate_node_acls=1']
    logging.info('Configuring iSCSI authentication')
    log_and_call(cmdline)


def save_iscsi_config():
    logging.info('Saving iSCSI configuration')
    log_and_call(['targetcli', 'saveconfig'])


@contextlib.contextmanager
def publish_to_iscsi(device):
    with transact(
        rollback=('saving iSCSI config', lambda _: save_iscsi_config())
    ), contextlib.ExitStack() as stack:
        backstore_name = stack.enter_context(create_iscsi_backstore(device))
        target_name = stack.enter_context(create_iscsi_target(backstore_name))
        configure_authentication(target_name)
        save_iscsi_config()
        yield target_name


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
    with transact(
        rollback=(
            f'cleaning up iSCSI config {config_path}',
            lambda _: os.remove(config_path)
        )
    ):
        yield config_path


@contextlib.contextmanager
def saved_config(path):
    old_path = f'{path}.old'
    if os.path.exists(old_path):
        logging.warning('Old config %s exists, removing', old_path)
        os.remove(old_path)

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
        os.remove(old_path)


@contextlib.contextmanager
def published_ipxe_config(output, config, testing=False):
    path = os.path.join(output, 'boot-test.ipxe' if testing else 'boot.ipxe')
    logging.info(f'Publishing{" testing" if testing else ""} iPXE config '
                 'to %s', path)
    with contextlib.ExitStack() as stack:
        stack.enter_context(saved_config(path))
        stack.enter_context(transact(
            rollback=(f'removing {path}', lambda _: os.remove(path))
        ))
        os.symlink(config, path)
        yield path


@contextlib.contextmanager
def reset_back_on_failure(vmm, vm):
    with transact(rollback=(None, lambda _: vmm.reset(vm))):
        yield


def reboot_and_check_test_vm(vmm, vm, host, timestamp):
    def booted_properly(host):
        if not is_accessible(host):
            return False
        try:
            cmdline = ['ssh', host, 'cat', '/etc/timestamp']
            output = log_and_output(cmdline).strip()
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


def try_reboot_if_idle(host):
    logging.info('Checking if host %s is idle', host)
    try:
        who = ssh(host, 'who', output=True,
                  options=('-o', 'ConnectTimeout=1')).strip()
    except Exception:
        logging.exception('Failed to check if host %s is idle', host)
        return

    if who:
        logging.info('Host %s is busy, skipping reboot', host)
    else:
        try:
            reboot(host)
        except Exception:
            logging.exception('Failed to reboot host %s', host)


def reboot_inactive_clients(vmm, args):
    snapshots = get_snapshots(vmm, args.ref_vm)

    for snapshot in snapshots:
        backstore_name = get_iscsi_backstore_name(snapshot)
        target_name = get_iscsi_target_name(backstore_name)
        sessions = get_dynamic_iscsi_sessions(target_name)
        for session in sessions:
            try:
                host = get_hostname(session)
            except Exception:
                logging.exception('Failed to get hostname from %s', session)
                continue
            logging.debug('Snapshot %s is used on %s in session %s',
                          snapshot, host, session)
            if host != args.test_host:
                try_reboot_if_idle(host)


def add_snapshot(args):
    vmm = Virsh()
    check_preconditions(vmm, args.ref_vm, args.ref_host)

    timestamp = generate_timestamp()
    with contextlib.ExitStack() as snapshot_stack:
        snapshot_disk = snapshot_stack.enter_context(vm_disk_snapshot(
            vmm, args.ref_vm, args.ref_host, timestamp, args.snapshot_size,
            args.cache_config
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

        if args.link_snapshot_copy:
            snapshot_stack.enter_context(
                link_snapshot_copy(snapshot_disk, args.link_snapshot_copy,
                                   non_volatile_pv(args.cache_config))
            )

        configure_caching(snapshot_disk, args.cache_config)

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
        ipxe_config = snapshot_stack.enter_context(
            published_ipxe_config(args.output, ipxe_config)
        )
        logging.info('Published iPXE config to %s', ipxe_config)

    if args.push:
        logging.info('Pushing update to inactive clients with reboot')
        reboot_inactive_clients(vmm, args)


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
        lines = sessions_input.read().split('\0')
        return list(filter(None, map(str.strip, lines)))


def get_hostname(session, host_re=re.compile(r'^.+\:(?P<hostname>.+)'
                                             r'_....-..-.._..-..-..$')):
    match = host_re.match(session)
    if not match:
        raise ValueError(f'Session name {session} did not match any hostname')
    return match.group('hostname')


def clean_snapshot(output, cache_config, name, force=False):
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
        os.remove(ipxe_config)

    artifacts = snapshot_artifacts_path(output, name)
    if os.path.exists(artifacts):
        logging.info('Cleaning snapshot artifacts at %s', artifacts)
        shutil.rmtree(artifacts)

    try:
        remove_iscsi_target(target_name)
    except Exception:
        logging.warning('Failed to remove iSCSI target %s', target_name)

    try:
        remove_iscsi_backstore(backstore_name)
    except Exception:
        logging.warning('Failed to remove iSCSI backstore %s', backstore_name)

    save_iscsi_config()

    cleanup_kpartx(name)

    if cache_config:
        delete_cache_record(cache_config, name)

    copy_name = snapshot_copy_name(name)
    if os.path.exists(copy_name):
        logging.info('Removing snapshot copy %s', copy_name)
        try:
            remove_lv(copy_name)
        except Exception:
            logging.warning('Failed to remove snapshot copy %s', copy_name)

    if is_lv_open(name):
        raise RuntimeError(f'LV {name} is still open')

    logging.info('LV %s is not open, proceeding with remove', name)
    remove_lv(name)

    cache_volume = cache_lv_name(name)
    if os.path.exists(cache_volume):
        logging.warning('Cache volume %s still exists, removing', cache_volume)
        remove_lv(cache_volume)


def clean_snapshots(args):
    vmm = Virsh()
    snapshots = get_snapshots(vmm, args.ref_vm)
    if not snapshots:
        return

    old, latest = snapshots[:-1], snapshots[-1]

    for snapshot in old:
        clean_snapshot(args.output, args.cache_config, snapshot,
                       force=args.force_old)

    if args.force_latest:
        logging.warning('Removing latest snapshot %s', latest)
        clean_snapshot(args.output, args.cache_config, latest, force=True)


def disable_cache_on(volume):
    try:
        logging.info('Disabling cache on %s', volume)
        log_and_call(['lvconvert', '--uncache', volume])
    except Exception:
        logging.exception('Failed to disable cache for %s', volume)


def cleanup_cache(args):
    vg = args.cache_config.volume_group
    for record in list_cache_records(args.cache_config):
        disable_cache_on(lv_path(vg, record))

    logging.info('Reducing VG %s, removing missing PVs', vg)
    log_and_call(['vgreduce', '--removemissing', vg])

    logging.info('Activating all LVs in VG %s', vg)
    log_and_call(['vgchange', '-ay', vg])


def enable_cache(args):
    if args.cleanup:
        cleanup_cache(args)

    cache_pv = args.cache_config.cache_pv
    logging.info('Creating cache PV %s', cache_pv)
    log_and_call(['pvcreate', '-y', cache_pv])

    vg = args.cache_config.volume_group
    logging.info('Adding cache PV %s to VG %s', cache_pv, vg)
    log_and_call(['vgextend', vg, cache_pv])

    for record in list_cache_records(args.cache_config):
        configure_caching(lv_path(vg, record), args.cache_config)


def disable_cache(args):
    vg = args.cache_config.volume_group
    for record in list_cache_records(args.cache_config):
        disable_cache_on(lv_path(vg, record))

    cache_pv = args.cache_config.cache_pv
    try:
        logging.info('Removing cache PV %s from VG %s', cache_pv, vg)
        log_and_call(['vgreduce', vg, cache_pv])
    except Exception:
        logging.exception('Failed to remove cache PV from VG')

    try:
        logging.info('Destroying cache PV %s', cache_pv)
        log_and_call(['pvremove', '-f', cache_pv])
    except Exception:
        logging.exception('Failed to destroy cache PV')


def config_parser(type_):
    def parser(value):
        with open(value) as config_input:
            return type_(**json.load(config_input))
    return parser


def config(value):
    with open(value) as config_input:
        return json.load(config_input)


def get_args(raw_args):
    config_parser = argparse.ArgumentParser(raw_args[0], add_help=False)
    config_parser.add_argument(
        '--config', type=config,
        help='Path to config file with all the options in JSON'
    )
    known_args, unknown_args = config_parser.parse_known_args(raw_args[1:])
    if known_args.config is not None:
        if unknown_args:
            config_parser.error(
                '--config is not compatible with other options'
            )
        return known_args.config
    else:
        return raw_args[1:]


def parse_args(raw_args):
    parser = argparse.ArgumentParser(raw_args[0])
    parser.add_argument('-v', '--verbose', action='count', default=0)
    parser.add_argument('-l', '--lock',
                        help='Lock specified file exclusively while '
                             'running an update')
    subparsers = parser.add_subparsers(
        metavar='subcommand', help='subcommand to execute', required=True
    )

    add_parser = subparsers.add_parser('add', help='Add new snapshot')
    add_parser.add_argument('-s', '--snapshot-size', default='5G')
    add_parser.add_argument('--cache-config', type=config_parser(CacheConfig))
    add_parser.add_argument('--to-copy', action='append')
    add_parser.add_argument('--chroot-script')
    add_parser.add_argument('--link-snapshot-copy',
                            help='Add symlink to snapshot suitable for '
                                 'deploying to this locaiton')
    add_parser.add_argument('--push', action='store_true',
                            help='Try to push update to inactive clients')

    add_parser.add_argument('ref_vm')
    add_parser.add_argument('ref_host')
    add_parser.add_argument('partitions_config',
                            type=config_parser(CowPartitionsConfig))
    add_parser.add_argument('output')
    add_parser.add_argument('test_vm')
    add_parser.add_argument('test_host')
    add_parser.set_defaults(func=add_snapshot)

    clean_parser = subparsers.add_parser('clean', help='Cleanup old snapshots')
    clean_parser.add_argument('--force-old', action='store_true')
    clean_parser.add_argument('--force-latest', action='store_true')
    clean_parser.add_argument('--cache-config',
                              type=config_parser(CacheConfig))
    clean_parser.add_argument('ref_vm')
    clean_parser.add_argument('output')
    clean_parser.set_defaults(func=clean_snapshots)

    enable_cache_parser = subparsers.add_parser(
        'enable_cache',
        help='Add cache PV to VG and enable cache for all the '
             'volumes configured to use with cache'
    )
    enable_cache_parser.add_argument('cache_config',
                                     type=config_parser(CacheConfig))
    enable_cache_parser.add_argument(
        '--cleanup', action='store_true',
        help='Shrink cached VG and uncache previously cached volumes, '
             'if necessary, useful for system startup scripts'
    )
    enable_cache_parser.set_defaults(func=enable_cache)

    disable_cache_parser = subparsers.add_parser(
        'disable_cache',
        help='Disable cache for all the cached volumes configured and '
             'remove cache PV out of VG'
    )
    disable_cache_parser.add_argument('cache_config',
                                      type=config_parser(CacheConfig))
    disable_cache_parser.set_defaults(func=disable_cache)

    return parser.parse_args(get_args(raw_args))


def configure_logging(args):
    levels = {
        0: logging.WARN,
        1: logging.INFO,
    }
    logging.basicConfig(
        level=levels.get(args.verbose, logging.DEBUG),
        format='%(levelname)-8s ' '%(message)s',
    )


@contextlib.contextmanager
def locked(args):
    if args.lock:
        with open(args.lock, 'w') as lock:
            try:
                logging.debug('Locking %s', args.lock)
                fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    logging.error('%s is already locked, exiting', args.lock)
                    sys.exit(2)
                raise
            else:
                logging.debug('Unlocking %s', args.lock)
                fcntl.lockf(lock, fcntl.LOCK_UN)
    else:
        yield


def main(raw_args):
    args = parse_args(raw_args)
    configure_logging(args)
    with locked(args):
        return args.func(args)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
