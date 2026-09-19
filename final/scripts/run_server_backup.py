"""Daily snapshot and restricted off-host transfer; any failed copy fails the job."""
import json
from pathlib import Path
import subprocess

from backup_server import backup


if __name__ == '__main__':
    result = backup('/opt/agi-saber', '/opt/agi-saber/backups', keep=7)
    path = Path(result['archive'])
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UserKnownHostsFile=/opt/agi-saber/backup-ssh/known_hosts',
               '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
               '-i', '/opt/agi-saber/backup-ssh/id_ed25519',
               'agisaberbackup@134.175.136.160', 'receive', path.name, result['sha256']]
    with path.open('rb') as source:
        completed = subprocess.run(command, stdin=source, capture_output=True, timeout=600, check=True)
    received = json.loads(completed.stdout)
    if received['sha256'] != result['sha256'] or received['bytes'] != result['bytes']:
        raise RuntimeError('Remote receipt differs from local archive')
    result['offsite'] = received
    print(json.dumps(result))
