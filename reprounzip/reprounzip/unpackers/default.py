from __future__ import unicode_literals

import shutil
import sys
import tarfile
import tempfile
import os
import re
import sqlite3
import subprocess

from reprounzip.common import FILE_WDIR
from reprounzip.utils import find_all_links
from reprounzip.unpackers.common import load_config, select_installer, \
    shell_escape


def makedir(path):
    if not os.path.exists(path):
        makedir(os.path.dirname(path))
        try:
            os.mkdir(path)
        except OSError:
            pass


def list_directories(pack):
    tmp = tempfile.mkdtemp(prefix='reprozip_')
    try:
        tar = tarfile.open(pack, 'r:*')
        try:
            tar.extract('METADATA/trace.sqlite3', path=tmp)
        except KeyError:
            sys.stderr.write("Pack doesn't have trace.sqlite3, can't create "
                             "working directories\n")
            return set()
        database = os.path.join(tmp, 'METADATA/trace.sqlite3')
        tar.close()
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        executed_files = cur.execute(
                '''
                SELECT name
                FROM opened_files
                WHERE mode = ?
                ''',
                (FILE_WDIR,))
        result = set(n for (n,) in executed_files)
        cur.close()
        conn.close()
        return result
    finally:
        shutil.rmtree(tmp)


def installpkgs(args):
    """Installs the necessary packages on the current machine.
    """
    pack = args.pack[0]

    # Loads config
    runs, packages, other_files = load_config(pack)

    installer = select_installer(pack, runs)

    # Installs packages
    r = installer.install(packages, assume_yes=args.assume_yes)
    if r != 0:
        sys.exit(r)


def create_chroot(args):
    """Unpacks the experiment in a folder so it can be run with chroot.

    All the files in the pack are unpacked; system files are copied only if
    they were not packed, and for /bin/sh and dependencies (if they were not
    packed).
    """
    pack = args.pack[0]
    target = args.target[0]
    if os.path.exists(target):
        sys.stderr.write("Error: Target directory exists\n")
        sys.exit(1)

    # Loads config
    runs, packages, other_files = load_config(pack)

    os.mkdir(target)
    root = os.path.abspath(os.path.join(target, 'root'))
    os.mkdir(root)

    # Checks that everything was packed
    packages_not_packed = [pkg for pkg in packages if not pkg.packfiles]
    if packages_not_packed:
        sys.stderr.write("Error: According to configuration, some files were "
                         "left out because they belong to the following "
                         "packages:\n")
        sys.stderr.write(''.join('    %s\n' % pkg
                                 for pkg in packages_not_packed))
        sys.stderr.write("Will copy files from HOST SYSTEM\n")
        for pkg in packages_not_packed:
            for ff in pkg.files:
                for f in find_all_links(ff.path):
                    if not os.path.exists(f):
                        sys.stderr.write(
                                "Missing file %s (from package %s) on host, "
                                "experiment will probably miss it\n" % (
                                    f, pkg.name))
                    dest = f
                    if dest[0] == '/':
                        dest = dest[1:]
                    dest = os.path.join(root, dest)
                    makedir(os.path.dirname(dest))
                    if os.path.islink(f):
                        os.symlink(os.readlink(f), dest)
                    else:
                        shutil.copy(f, dest)

    # Unpacks files
    tar = tarfile.open(pack, 'r:*')
    if any('..' in m.name for m in tar.getmembers()):
        sys.stderr.write("Error: Tar archive contains invalid pathnames\n")
        sys.exit(1)
    members = [m for m in tar.getmembers() if m.name.startswith('DATA/')]
    for m in members:
        m.name = m.name[5:]
    tar.extractall(root, members)
    tar.close()

    # Copies /bin/sh + dependencies
    fmt = re.compile(r'^\t(?:[^ ]+ => )?([^ ]+) \([x0-9a-z]+\)$')
    p = subprocess.Popen(['ldd', '/bin/sh'], stdout=subprocess.PIPE)
    try:
        for l in p.stdout:
            l = l.decode('ascii')
            m = fmt.match(l)
            f = m.group(1)
            if not os.path.exists(f):
                continue
            dest = f
            if dest[0] == '/':
                dest = dest[1:]
            dest = os.path.join(root, dest)
            makedir(os.path.dirname(dest))
            if not os.path.exists(dest):
                shutil.copy(f, dest)
    finally:
        p.wait()
    assert p.returncode == 0
    makedir(os.path.join(root, 'bin'))
    dest = os.path.join(root, 'bin/sh')
    if not os.path.exists(dest):
        shutil.copy('/bin/sh', dest)

    # Makes sure all the directories used as working directories exist
    # (they already do if files from them are used, but empty directories do
    # not get packed inside a tar archive)
    for directory in list_directories(pack):
        makedir(os.path.join(root, directory[1:]))

    # Writes start script
    with open(os.path.join(target, 'script.sh'), 'w') as fp:
        fp.write('#!/bin/sh\n\n')
        for run in runs:
            cmd = 'cd %s && ' % shell_escape(run['workingdir'])
            # FIXME : Use exec -a or something if binary != argv[0]
            cmd += ' '.join(
                    shell_escape(a)
                    for a in [run['binary']] + run['argv'][1:])
            userspec = '%s:%s' % (run.get('uid', 1000), run.get('gid', 1000))
            fp.write('chroot --userspec=%s %s /bin/sh -c %s\n' % (
                     userspec,
                     shell_escape(root),
                     shell_escape(cmd)))

    print("Experiment set up, run %s to start" % (
          os.path.join(target, 'script.sh')))


def setup(subparsers, general_options):
    # Install the required packages
    parser_installpkgs = subparsers.add_parser(
            'installpkgs', parents=[general_options],
            help="Installs the required packages on this system")
    parser_installpkgs.add_argument('pack', nargs=1,
                                    help="Pack to process")
    parser_installpkgs.add_argument(
            '-y', '--assume-yes',
            help="Assumes yes for package manager's questions (if supported)")
    parser_installpkgs.set_defaults(func=installpkgs)

    # Unpacks all the file so the experiment can be run with chroot
    parser_chroot = subparsers.add_parser(
            'chroot', parents=[general_options],
            help="Unpacks the files so the experiment can be run with chroot")
    parser_chroot.add_argument('pack', nargs=1,
                               help="Pack to extract")
    parser_chroot.add_argument('target', nargs=1,
                               help="Directory to create")
    parser_chroot.set_defaults(func=create_chroot)
