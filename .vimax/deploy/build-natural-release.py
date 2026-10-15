"""Prepare an immutable source bundle, then explicitly build two new release images.

prepare is local only. build uploads the prepared bundle and creates one new
release; it never starts containers, imports runs, changes current or publishes.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tarfile


RUNTIME_BASE_PATHS = ('dialogue/backend/requirements.txt', 'agents', 'interfaces', 'utils', 'agent_runtime')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    return result.stdout.decode('utf-8').strip()


def identity(root, revision, previous):
    require(re.fullmatch(r'[0-9a-f]{7}', revision), 'revision must be 7 lowercase commit hex characters')
    require(re.fullmatch(r'[0-9]{8}-[0-9a-f]{7}', previous), 'expected_previous must be YYYYMMDD-7hex')
    commit = git(root, 'rev-parse', revision + '^{commit}')
    require(commit == git(root, 'rev-parse', 'HEAD'), 'Build the current committed HEAD only')
    require(commit[:7] == revision, 'revision does not resolve to the exact commit prefix')
    require(not git(root, 'status', '--porcelain', '--untracked-files=no'), 'Tracked worktree/index must be clean')
    old_commit = git(root, 'rev-parse', previous[-7:] + '^{commit}')
    require(old_commit != commit, 'New release must have a different commit from expected_previous')
    changed = git(root, 'diff', '--name-only', old_commit, commit, '--', *RUNTIME_BASE_PATHS)
    require(not changed, 'Old-image backend reuse is invalid: runtime dependencies/source changed: ' + changed)
    return {'schema_version': 1, 'commit': commit, 'revision': revision,
            'batch': '20261015-' + revision, 'expected_previous': previous,
            'base_commit': old_commit, 'backend_base_image': 'vimax-dialogue-backend:' + previous}


def dist_files(root):
    directory = root / 'web/dist'
    require((directory / 'index.html').is_file(), 'Build web/dist first with npm --prefix web run build')
    require(not directory.is_symlink(), 'dist must not be a symlink')
    files = {}
    for path in sorted(directory.rglob('*')):
        require(not path.is_symlink(), 'dist cannot contain symlinks')
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def validate_members(bundle):
    for member in bundle.getmembers():
        path = PurePosixPath(member.name)
        require(not path.is_absolute() and '..' not in path.parts and '\\' not in member.name,
                'Unsafe source archive path')
        require(member.isfile() or member.isdir(), 'Source archive links/devices are not allowed')
        require(not any(part in ('.git', '.env', '.runtime', 'node_modules') for part in path.parts),
                'Private/runtime directory in source archive')
        require(not (path.name.endswith(('.sqlite', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))
                     or path.name in ('runtime-model.yaml', 'agent.local.yaml', 'release.env', 'access.env')
                     or path.name.startswith('.env.')), 'Private/runtime file in source archive')


def prepare(root, revision, previous):
    record = identity(root, revision, previous)
    files = dist_files(root)
    destination = root / '.vimax/deploy/builds' / record['batch']
    destination.mkdir(parents=True, exist_ok=False)
    source = destination / 'source.tar'
    subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', '-o', str(source), record['commit']], check=True)
    with tarfile.open(source, 'a') as bundle:
        validate_members(bundle)
        require(not any(p.name.startswith('web/dist/') for p in bundle.getmembers()),
                'web/dist must be untracked; avoid duplicate archived members')
        for name, value in files.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(value), 0o644, 0
            bundle.addfile(item, io.BytesIO(value))
    require(files == dist_files(root), 'dist changed during bundle creation')
    require(record == identity(root, revision, previous), 'Commit/worktree changed during preparation')
    payload = destination / 'source.tar.gz'
    with source.open('rb') as incoming, payload.open('xb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as compressed:
            shutil.copyfileobj(incoming, compressed)
    record.update(source_sha256=digest(payload.read_bytes()),
                  dist_sha256={name: digest(value) for name, value in files.items()},
                  helper_sha256=digest(Path(__file__).read_bytes()))
    with (destination / 'build-input.json').open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    print('Prepared locally: ' + str(destination))
    return destination


def load_prepared(root, revision, previous):
    current = identity(root, revision, previous)
    directory = root / '.vimax/deploy/builds' / current['batch']
    record = json.loads((directory / 'build-input.json').read_bytes())
    require(all(record.get(k) == v for k, v in current.items()), 'Prepared commit/previous differs')
    require(record['source_sha256'] == digest((directory / 'source.tar.gz').read_bytes()), 'Bundle hash changed')
    require(record['dist_sha256'] == {k: digest(v) for k, v in dist_files(root).items()}, 'Prepared dist changed')
    require(record['helper_sha256'] == digest(Path(__file__).read_bytes()), 'Builder changed after preparation')
    with tarfile.open(directory / 'source.tar.gz', 'r:gz') as bundle:
        validate_members(bundle)
    return directory, record


def previous_guard(record):
    old = '/opt/vimax/releases/' + record['expected_previous']
    return 'test "$(readlink -f /opt/vimax/current)" = ' + shlex.quote(old)


def preflight_script(record):
    release = '/opt/vimax/releases/' + record['batch']
    old = record['expected_previous']
    return '\n'.join(['set -eu', previous_guard(record),
        "test \"$(docker inspect vimax-dialogue-backend-1 --format '{{.Config.Image}}')\" = " + shlex.quote('vimax-dialogue-backend:' + old),
        "test \"$(docker inspect vimax-dialogue-workbench-1 --format '{{.Config.Image}}')\" = " + shlex.quote('vimax-dialogue-web:' + old),
        "test \"$(docker inspect vimax-dialogue-backend-1 --format '{{.State.Health.Status}}')\" = healthy",
        "test \"$(docker inspect vimax-dialogue-workbench-1 --format '{{.State.Running}}')\" = true",
        "test \"$(docker inspect vimax-dialogue-backend-1 --format '{{.Image}}')\" = \"$(docker image inspect " + shlex.quote(record['backend_base_image']) + " --format '{{.Id}}')\"",
        'test -f /opt/vimax/current/release.env',
        'test ! -e ' + shlex.quote(release), 'test ! -L ' + shlex.quote(release),
        'if docker image inspect ' + shlex.quote('vimax-dialogue-backend:' + record['batch']) + ' >/dev/null 2>&1; then exit 31; fi',
        'if docker image inspect ' + shlex.quote('vimax-dialogue-web:' + record['batch']) + ' >/dev/null 2>&1; then exit 32; fi',
        'mkdir -m 700 ' + shlex.quote(release)])


def build_script(record):
    release = '/opt/vimax/releases/' + record['batch']
    backend = 'vimax-dialogue-backend:' + record['batch']
    web = 'vimax-dialogue-web:' + record['batch']
    recipe = 'FROM ' + record['backend_base_image'] + '\nCOPY dialogue/backend /app/dialogue/backend\n'
    return '\n'.join(['set -eu', previous_guard(record), 'cd ' + shlex.quote(release),
        'test "$(sha256sum source.tar.gz | cut -d\' \' -f1)" = ' + shlex.quote(record['source_sha256']),
        'tar -xzf source.tar.gz', 'test -f web/dist/index.html',
        'cp /opt/vimax/current/release.env release.env', 'chmod 600 release.env',
        "sed -i 's/^VIMAX_RELEASE=.*/VIMAX_RELEASE=" + record['batch'] + "/' release.env",
        'test "$(grep -c \"^VIMAX_RELEASE=' + record['batch'] + '$\" release.env)" = 1',
        "printf '%s' " + shlex.quote(recipe) + ' > deploy/Dockerfile.evolution-build',
        'docker build -t ' + shlex.quote(backend) + ' -f deploy/Dockerfile.evolution-build .',
        'docker build -t ' + shlex.quote(web) + ' -f deploy/Dockerfile.web .',
        'docker run --rm -e SCRIPT_GENERATOR_DATA_DIR=/tmp/evolution-build-tests ' + shlex.quote(backend) +
        ' python -m unittest discover -s dialogue/backend/tests -q',
        previous_guard(record),
        "docker image inspect " + shlex.quote(backend) + " --format '{{.Id}}' > backend-image-id.txt",
        "docker image inspect " + shlex.quote(web) + " --format '{{.Id}}' > web-image-id.txt",
        "printf '%s\\n' 'Both images built and backend tests passed; no service was started.'"])


def remote_command(client, script, password):
    _, output, _ = client.exec_command('sh -c ' + shlex.quote(script), timeout=1800)
    output.channel.set_combine_stderr(True)
    for line in output:
        print(line.replace(password, '[redacted]'), end='', flush=True)
    require(output.channel.recv_exit_status() == 0,
            'Remote step failed; preserve the new release for inspection; no automatic cleanup/retry')


def build(root, revision, previous, host, port, user):
    directory, record = load_prepared(root, revision, previous)
    require(host and not host.startswith('-'), 'Set --host or VIMAX_TASK_SSH_HOST')
    require(1 <= port <= 65535, 'Invalid SSH port')
    password = os.environ.get('VIMAX_TASK_SSH_PASSWORD', '')
    require(password, 'Set VIMAX_TASK_SSH_PASSWORD in the environment')
    import paramiko
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, port=port, username=user, password=password,
                       timeout=20, allow_agent=False, look_for_keys=False)
        remote_command(client, preflight_script(record), password)
        release = '/opt/vimax/releases/' + record['batch']
        with client.open_sftp() as sftp:
            for name in ('source.tar.gz', 'build-input.json'):
                sftp.put(str(directory / name), release + '/' + name)
                sftp.chmod(release + '/' + name, 0o600)
        remote_command(client, build_script(record), password)
        with client.open_sftp() as sftp:
            image_ids = {}
            for role in ('backend', 'web'):
                with sftp.file(release + '/' + role + '-image-id.txt', 'rb') as stream:
                    value = stream.read().decode('ascii').strip()
                require(re.fullmatch(r'sha256:[0-9a-f]{64}', value), 'Invalid remote image ID receipt')
                image_ids[role] = value
        receipt = {**record, 'image_ids': image_ids, 'result': 'built_and_backend_tests_passed',
                   'service_started': False, 'remote_release': release}
        with (directory / 'build-receipt.json').open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        print('Build receipt: ' + str(directory / 'build-receipt.json'))
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'build'))
    parser.add_argument('--revision', required=True)
    parser.add_argument('--expected-previous', required=True)
    parser.add_argument('--host', default=os.environ.get('VIMAX_TASK_SSH_HOST'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('VIMAX_TASK_SSH_PORT', '22')))
    parser.add_argument('--user', default=os.environ.get('VIMAX_TASK_SSH_USER', 'root'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.mode == 'prepare':
        prepare(root, args.revision, args.expected_previous)
    else:
        build(root, args.revision, args.expected_previous, args.host, args.port, args.user)


if __name__ == '__main__':
    main()
