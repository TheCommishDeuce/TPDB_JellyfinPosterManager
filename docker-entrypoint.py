"""Initialize writable container directories and run the app as the requested user."""
import os
from pathlib import Path
import re
import sys


def identity(name, default):
    value = os.environ.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", value) or not 0 < int(value) < 4294967295:
        raise ValueError(f'{name} must be a positive, non-root numeric ID')
    return int(value)


def permissions_mask():
    value = os.environ.get('UMASK', '022')
    if not re.fullmatch(r'0?[0-7]{3}', value):
        raise ValueError('UMASK must be three octal digits, optionally prefixed with 0')
    return int(value, 8)


def prepare_directory(directory, uid, gid):
    path = Path(directory)
    if path.resolve() in tuple(Path(root).resolve() for root in ('/', '/app', '/home')):
        raise ValueError(f'Writable directory must be a dedicated runtime directory: {path}')
    if path.is_symlink():
        raise ValueError(f'Writable directory must not be a symlink: {path}')
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(path, uid, gid)
        # Repair existing volume contents when the configured IDs change.
        for root, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                child = Path(root) / name
                if not child.is_symlink():
                    os.chown(child, uid, gid, follow_symlinks=False)


def main():
    uid, gid = identity('PUID', 99), identity('PGID', 100)
    mask = permissions_mask()
    if os.geteuid() != 0 and (os.geteuid(), os.getegid()) != (uid, gid):
        raise ValueError('When using --user, its IDs must match PUID and PGID')
    os.umask(mask)
    for name, default in (
        ('HOME', '/home/appuser'), ('APP_STATE_DIR', '/app/data'),
        ('CACHE_DIR', '/app/cache'), ('LOG_DIR', '/app/logs'),
    ):
        prepare_directory(os.environ.get(name, default), uid, gid)
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    command = sys.argv[1:] or ['python', 'app.py']
    os.execvp(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(f'Container initialization failed: {error}', file=sys.stderr)
        sys.exit(1)
