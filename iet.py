#!/usr/bin/env python

import glob, os, re, sys, subprocess
from get_disks import get_disks


def format_attrs(attrs):
  return ','.join('{}={}'.format(key, value) for key, value in attrs.iteritems())


class Volume:
  def __init__(self, path):
    self.path = path
    self.tid  = None

  def __str__(self):
    return 'volume: {}, tid: {}'.format(self.path, self.tid)


class LUN:
  def __init__(self, number):
    self.number = number
    self.params = {}

  @staticmethod
  def from_live(number, params):
    lun      = LUN(number)
    lun.path = params['path']
    for src, dst in {'iotype': 'Type', 'iomode': 'IOMode'}.iteritems():
      if src in params:
        lun.params[dst] = params[src]
    return lun

  @staticmethod
  def from_config(number, params):
    lun = LUN(number)
    lun.params = params.copy()
    lun.path   = lun.params['Path']
    del lun.params['Path']
    return lun

  def format_params(self):
    rv = 'Path={}'.format(self.path)
    for key, value in self.params.iteritems():
      rv += ',{}={}'.format(key, value)
    return rv


class Target:
  def __init__(self, name):
    self.name     = name
    self.tid      = None
    self.luns     = []
    self.sessions = {}
    self.last     = False

  @staticmethod
  def from_live(name, tid, params):
    target = Target(name)
    target.tid      = tid
    target.params   = {}
    for key, default in {'NOPInterval': '60', 'NOPTimeout': '5'}.iteritems():
      target.params[key] = params.get(key, default)
    return target

  @staticmethod
  def from_config(name, luns, params):
    target = Target(name)
    target.luns = luns[:]
    target.params = params.copy()
    return target

  def format_params(self):
    return (','.join('{}={}'.format(key, value)
        for key, value in self.params.iteritems()))

  def __str__(self):
    rv = 'target {} {} last={}\n'.format(self.tid, self.name, self.last)
    rv += ' params: {}\n'.format(self.format_params())
    for lun in self.luns:
      rv += '  lun: {} {}\n'.format(lun.number, lun.format_params())
    for sid, session in self.sessions.iteritems():
      rv += '  session: {} {}\n'.format(session.sid, session.initiator)
      for cid, connection in session.connections.iteritems():
        rv += '    connection: {} {}\n'.format(cid, format_attrs(connection.attrs))
    return rv


class Connection:
  def __init__(self, cid, attrs):
    self.cid   = cid
    self.attrs = attrs


class Session:
  def __init__(self, sid, initiator):
    self.sid         = sid
    self.initiator   = initiator
    self.connections = {}


def get_live_targets():
  FILE = '/proc/net/iet/volume'

  tid, target = [None] * 2
  targets = {}

  with open(FILE, 'r') as infile:
    for line in infile:
      is_tid = re.search('^tid:(\d+)\s+name:(.+)$', line)
      is_lun = re.search('^\s+lun:(\d+)\s+(.+)$', line)
      if is_tid:
        tid, name = is_tid.group(1, 2)
        target_params = {}
        for param in call_ietadm('show', tid).strip().split('\n'):
          key, value = param.split('=', 2)
          target_params[key] = value
        target = Target.from_live(name, tid, target_params)
        targets[tid] = target
      else:
        assert(target)
        assert(is_lun)
        lun_id = is_lun.group(1)
        params = {}
        for param in is_lun.group(2).split(' '):
          key, value = param.split(':')
          params[key] = value
        target.luns.append(LUN.from_live(lun_id, params))

  FILE = '/proc/net/iet/session'

  target, sid, session = [None] * 3

  with open(FILE, 'r') as infile:
    for line in infile:
      is_tid = re.search('^tid:(\d+)\s+name:(.+)$', line)
      is_sid = re.search('^\s+sid:(\d+)\s+initiator:(.+)$', line)
      is_cid = re.search('^\s+cid:(\d+)\s+(.+)$', line)
      if is_tid != None:
        if target != None and session != None:
          target.sessions[sid] = session
          target, sid, session = [None] * 3
        tid, name = is_tid.group(1, 2)
        assert(tid in targets)
        target = targets[tid]
        assert(target.name == name)
      elif is_sid != None:
        if session != None:
          target.sessions[sid] = session
        sid, session = [None] * 2
        sid, initiator = is_sid.group(1, 2)
        session = Session(sid, initiator)
      else:
        assert(is_cid != None)
        cid = is_cid.group(1)
        attrs = dict(map(lambda attr: attr.split(':', 2), is_cid.group(2).split(' ')))
        connection = Connection(cid, attrs)
        session.connections[cid] = connection

  if target != None and session != None:
    target.sessions[sid] = session
    target, sid, session = [None] * 3
  return targets


def get_config_targets(filename):
  targets = {}
  name   = None
  luns   = []
  params = {}

  with open(filename, 'r') as infile:
    for line in infile:
      if line.strip().startswith('#') or len(line.strip()) == 0:
        continue
      is_target = re.search('^Target\s+(.+)$', line)
      is_lun = re.search('^\s+Lun\s+(\d+)\s+(.+)$', line)
      if is_target:
        if name != None:
          targets[name] = Target.from_config(name, luns, params)
          name   = None
          luns   = []
          params = {}
        name = is_target.group(1)
        assert(name not in targets)
      elif is_lun:
        assert(name)
        lun_id = is_lun.group(1)
        lun_params = dict(map(lambda param: param.split('='),
                              is_lun.group(2).split(',')))
        luns.append(LUN.from_config(lun_id, lun_params))
      else:
        key, value = line.strip().split(' ')
        assert(name)
        assert(key not in params)
        params[key] = value
  if name != None:
    targets[name] = Target.from_config(name, luns, params)
    name   = None
    luns   = []
    params = {}
  return targets


def put_config_targets(targets, filename):
  with open(filename, 'w') as output:
    for target in targets:
      print >> output, 'Target {}'.format(target.name)
      for lun in target.luns:
        print >> output, '  Lun {} {}'.format(lun.number, lun.format_params())
      for key, value in target.params.iteritems():
        print >> output, '  {} {}'.format(key, value)


def call_ietadm(op, tid, lun=None, sid=None, cid=None, params=None):
  cmdline = ['/usr/sbin/ietadm',
             '--op={}'.format(op),
             '--tid={}'.format(tid)]
  if lun != None:
    cmdline.append('--lun={}'.format(lun))
  if sid != None:
    cmdline.append('--sid={}'.format(sid))
  if cid != None:
    cmdline.append('--cid={}'.format(cid))
  if params != None:
    cmdline += ['--params', params]
  process = subprocess.Popen(cmdline, stdout=subprocess.PIPE)
  (stdout, stderr) = process.communicate()
  if process.returncode != 0:
    raise Exception('"{}" failed with code {}'.format(' '.join(cmdline), rv))
  return stdout


def get_tid(name):
  for tid, target in get_live_targets().iteritems():
    if target.name == name:
      return tid


def get_volume_name(volume):
  return os.path.basename(volume)


def build_live_target(volume):
  name = get_volume_name(volume.path)
  call_ietadm('new', 0, params='Name={}'.format(name))
  tid = get_tid(name)
  assert(tid)

  target = Target.from_live(name, tid, {})
  lun = LUN.from_live('0', {'path': volume.path,
                            'iotype': 'fileio',
                            'iomode': 'ro'})
  target.luns = [lun]

  call_ietadm('update', tid, params=target.format_params())
  call_ietadm('new', tid, lun=lun.number, params=lun.format_params())

  volume.tid = tid
  return (tid, target)


def remove_live_target(target):
  assert(len(target.sessions) == 0)
  call_ietadm('delete', target.tid)


def parse_config(filename):
  config = {}
  with open(filename, 'r') as conf_file:
    for line in conf_file:
      if line.strip().startswith('#') or len(line.strip()) == 0:
        continue
      key, value = line.strip().split('=', 2)
      config[key] = value
  return config


def get_disk_pattern(config_filename):
  config = parse_config(config_filename)
  ref_vm_disks = get_disks(config['REF_VM_PATH'])
  assert(len(ref_vm_disks) == 1)
  return '{}-{}*'.format(ref_vm_disks[0], config['TIMESTAMP_SUFFIX'])


def get_volumes(disk_pattern):
  volumes = {}
  for vol_path in glob.glob(disk_pattern):
    volumes[vol_path] = Volume(vol_path)
  return volumes


def is_interesting(disk_pattern, target):
  return any(map(lambda lun: re.match(disk_pattern, lun.path),
                 target.luns))


def get_interesting(disk_pattern, live_targets):
  rv = {}
  for tid, target in live_targets.iteritems():
    if is_interesting(disk_pattern, target):
      rv[tid] = target
  return rv


def take_volumes(volumes, targets):
  for tid, target in targets.iteritems():
    for lun in target.luns:
      volumes[lun.path].tid = tid


def free_targets(volumes, targets):
  for tid in targets.keys():
    target = targets[tid]
    if len(target.sessions) == 0 and not target.last:
      remove_live_target(target)
      for lun in target.luns:
        volumes[lun.path].tid = None
      del targets[tid]


def free_volumes(volumes):
  for volume in volumes.itervalues():
    if volume.tid == None:
      cmdline = ['/sbin/lvremove', '-f', volume.path]
      subprocess.call(cmdline)


def error(msg):
  print >> sys.stderr, msg
  sys.exit(1)


if __name__ == '__main__':
  if len(sys.argv) != 2:
    error("usage: {} <config>".format(sys.argv[0]))
  config = sys.argv[1]
  disk_pattern = get_disk_pattern(config)

  volumes = get_volumes(disk_pattern)
  live_targets = get_interesting(disk_pattern, get_live_targets())

  take_volumes(volumes, live_targets)

  if len(volumes.keys()) == 0:
    error('no volumes, nothing to do')
  last_volume = volumes[sorted(volumes.keys())[-1]]

  if last_volume.tid == None:
    (tid, target) = build_live_target(last_volume)
    live_targets[tid] = target
    target.last = True
  else:
    live_targets[last_volume.tid].last = True

  free_targets(volumes, live_targets)
  free_volumes(volumes)

  iet_config = '/etc/iet/ietd.conf'
  config_targets = get_config_targets(iet_config)

  final_targets = []
  for target in config_targets.itervalues():
    if not is_interesting(disk_pattern, target):
      final_targets.append(target)

  for target in live_targets.itervalues():
    if target.last:
      final_targets.append(target)

  new_config = '{}.new'.format(iet_config)
  put_config_targets(final_targets, new_config)
  os.rename(new_config, iet_config)
