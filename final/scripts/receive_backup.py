"""Restricted SSH receiver: accept checksum-bound archives, never shell commands."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile


def receive(directory, command, stream, keep=7):
    args = shlex.split(command)
    if (len(args) != 3 or args[0] != 'receive'
            or not re.fullmatch(r'agi-saber-\d{8}T\d{12}Z\.tar\.gz', args[1])
            or not re.fullmatch(r'[a-f0-9]{64}', args[2])):
        raise ValueError('Only checksum-bound backup reception is allowed')
    directory = Path(directory).resolve()
    target = directory / args[1]
    digest, size = hashlib.sha256(), 0
    # No existing archive can be replaced, even with the same timestamp.
    pending = tempfile.NamedTemporaryFile(prefix='.receiving-', dir=directory, delete=False)
    path = Path(pending.name)
    try:
        with pending:
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > 2 * 1024 ** 3:
                    raise ValueError('Archive exceeds receiver limit')
                digest.update(block)
                pending.write(block)
            pending.flush()
            os.fsync(pending.fileno())
            if digest.hexdigest() != args[2]:
                raise ValueError('Archive checksum mismatch')
        os.link(path, target)  # Atomic create, never overwrite an existing file.
    finally:
        path.unlink(missing_ok=True)
    for old in sorted(directory.glob('agi-saber-*.tar.gz'), reverse=True)[keep:]:
        if old.is_file() and not old.is_symlink():
            old.unlink()
    return {'archive': target.name, 'bytes': size, 'sha256': digest.hexdigest()}


if __name__ == '__main__':
    os.umask(0o077)
    try:
        print(json.dumps(receive('/var/lib/agi-saber-backup/archives',
                                 os.environ.get('SSH_ORIGINAL_COMMAND', ''), sys.stdin.buffer)))
    except Exception as exc:
        print(type(exc).__name__ + ': backup rejected', file=sys.stderr)
        raise SystemExit(1)
