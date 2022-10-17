import re
import sys


def get_disks(filename):
    config = {}
    with open(filename, 'r') as conf_file:
        exec(conf_file.read(), config)

    disks = []
    diskre = "(.+?),raw"
    for disk in config.get("disk", []):
        if res := re.match(diskre, disk):
            disks.append(res.group(1))
    return disks


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'usage: {sys.argv[0]} <config>', file=sys.stderr)
        sys.exit(1)

    print(':'.join(get_disks(sys.argv[1])))
