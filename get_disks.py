import sys, re

def get_disks(filename):
  config = {}
  with open(filename, 'r') as conf_file:
    exec(conf_file.read(), config)

  disks = []
  diskre = "phy:/(.+?),"
  for disk in config.get("disk", []):
    res = re.match(diskre, disk)
    if res != None:
      disks.append(res.group(1))
  return disks

if __name__ == '__main__':
  if len(sys.argv) != 2:
    print >> sys.stderr, "usage: {} <config>".format(sys.argv[0])
    sys.exit(1)

  print ":".join(get_disks(sys.argv[1]))
