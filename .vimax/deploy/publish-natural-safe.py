"""One-batch guarded publisher. Local client uploads; remote mode never loads secrets."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

BATCH = '20261015-cd9c336'
OLD = '/opt/vimax/releases/20261014-a335ea6'
RELEASE = '/opt/vimax/releases/' + BATCH
STATE = Path('/opt/vimax/shared/state')
ARCHIVE = STATE / 'dialogue-versions' / BATCH
BACKEND = 'vimax-dialogue-backend-1'
WORKBENCH = 'vimax-dialogue-workbench-1'
IMAGE = 'vimax-dialogue-backend:' + BATCH
WEB_IMAGE = 'vimax-dialogue-web:' + BATCH
VERSIONS = ('V01', 'V02')
SELECTED = 'V02'


def family_number(label):
    match = re.fullmatch(r'V([0-9]{2,})(?:-R[0-9]{3,})?', label)
    assert match, 'Expected version label VNN or VNN-RNNN'
    return int(match[1])


def configure(revision, selected, versions, previous='20261014-a335ea6'):
    global BATCH, OLD, RELEASE, IMAGE, WEB_IMAGE, ARCHIVE, VERSIONS, SELECTED
    assert re.fullmatch('[0-9a-f]{7}', revision), 'Expected 7-character commit revision'
    assert versions and len(set(versions)) == len(versions)
    assert all(family_number(v) >= 0 for v in versions) and selected in versions
    assert re.fullmatch('[0-9]{8}-[0-9a-f]{7}', previous), 'Expected previous YYYYMMDD-7hex release'
    BATCH = '20261015-' + revision
    RELEASE = '/opt/vimax/releases/' + BATCH
    OLD = '/opt/vimax/releases/' + previous
    IMAGE = 'vimax-dialogue-backend:' + BATCH
    WEB_IMAGE = 'vimax-dialogue-web:' + BATCH
    ARCHIVE = STATE / 'dialogue-versions' / BATCH
    VERSIONS, SELECTED = tuple(versions), selected


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def command(*parts):
    result = subprocess.run(list(parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {parts[:4]}\n{result.stderr[-3000:]}')
    return result.stdout.strip()


def compose(release, *parts):
    return command('docker', 'compose', '-p', 'vimax-dialogue', '--env-file',
                   release + '/release.env', '-f', release + '/deploy/compose.yaml', *parts)


def is_running(container):
    return command('docker', 'inspect', container, '--format', '{{.State.Running}}') == 'true'


def health():
    for _ in range(40):
        value = command('docker', 'inspect', BACKEND, '--format', '{{.State.Health.Status}}')
        if value == 'healthy':
            command('docker', 'exec', BACKEND, 'python', '-c',
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9014/api/health',timeout=5)")
            return
        time.sleep(2)
    raise RuntimeError('Backend health check failed')


def database_rows(state):
    with closing(sqlite3.connect('file:' + str(state / 'dialogue/app.sqlite3') + '?mode=ro', uri=True)) as db:
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        return dict(db.execute('select id,run_json from sessions'))


def snapshot(state, destination):
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    (destination / 'dialogue').mkdir(mode=0o700)
    with closing(sqlite3.connect('file:' + str(state / 'dialogue/app.sqlite3') + '?mode=ro', uri=True)) as source:
        # Opening a host SQLite 3.7 connection alone does not initialize WAL/SHM.
        # Perform a real read first, and keep that connection open throughout the
        # container backup so the read-only mount can use its shared WAL state.
        integrity = source.execute('pragma integrity_check').fetchall()
        assert integrity == [('ok',)], 'Source database integrity check failed'
        if hasattr(source, 'backup'):
            with closing(sqlite3.connect(str(destination / 'dialogue/app.sqlite3'))) as target:
                source.backup(target)
                assert target.execute('pragma integrity_check').fetchone()[0] == 'ok'
        else:
            # Python 3.6 has no Connection.backup(). Use the already inspected
            # backend image's modern SQLite API; never copy a live WAL database.
            expression = (
                "import sqlite3;from contextlib import closing;"
                "s=sqlite3.connect('file:/source/app.sqlite3?mode=ro',uri=True);"
                "d=sqlite3.connect('/backup/app.sqlite3');s.backup(d);"
                "assert d.execute('pragma integrity_check').fetchone()[0]=='ok';"
                "d.close();s.close()")
            command('docker', 'run', '--rm', '-v', (state / 'dialogue').as_posix() + ':/source:ro',
                    '-v', (destination / 'dialogue').as_posix() + ':/backup',
                    IMAGE, 'python', '-c', expression)
            database_rows(destination)  # Reopen on the host and check integrity/schema.
    (destination / 'dialogue/app.sqlite3').chmod(0o600)
    shutil.copy2(state / 'sessions.json', destination / 'sessions.json')


def verify_archive(directory):
    manifest = json.loads((directory / 'sha256.json').read_text(encoding='utf-8'))
    actual = {p.relative_to(directory).as_posix(): digest(p)
              for p in directory.rglob('*') if p.is_file() and p.name != 'sha256.json'}
    assert actual == manifest, 'Archive hash manifest mismatch'
    return manifest


def import_runs(state, working):
    working.mkdir(parents=True, exist_ok=True)
    for version in VERSIONS:
        print('Offline import ' + version, flush=True)
        template_version = version if (ARCHIVE / version / 'template.json').exists() else SELECTED
        selected_arg = ['--selected'] if version == SELECTED else []
        command('docker', 'run', '--rm', '-v', state.as_posix() + ':/app/.vimax',
                '-v', working.as_posix() + ':/app/.working_dir', '-v', ARCHIVE.as_posix() + ':/verification:ro',
                IMAGE, 'python', '/verification/import-run.py', '--root', '/app', '--db',
                '/app/.vimax/dialogue/app.sqlite3', '--run', '/verification/' + version + '/run.json',
                '--template', '/verification/' + template_version + '/template.json', '--version', version, *selected_arg)


def verify_state(state, originals, original_index):
    rows = database_rows(state)
    expected_ids = set(originals)
    for version in VERSIONS:
        reviewed = json.loads((ARCHIVE / version / 'run.json').read_text(encoding='utf-8'))
        expected_ids.add(reviewed['id'])
        actual = json.loads(rows[reviewed['id']])
        for key in ('messages', 'status', 'config', 'errors', 'quality_review', 'candidate_versions',
                    'version_record', 'program_review_deferred', 'program_review_history'):
            assert actual.get(key) == reviewed.get(key), f'{version} changed: {key}'
        if family_number(version) >= 4:
            publication = actual.get('publication', {})
            review = json.loads((ARCHIVE / version / 'review-index.json').read_text(encoding='utf-8'))
            assert publication.get('read_only') is True
            assert publication.get('source_run_sha256') == digest(ARCHIVE / version / 'run.json')
            assert publication.get('human_verdict') == review['verdict']
        assert len(actual['messages']) == 150
    assert set(rows) == expected_ids, 'Unexpected record creation/deletion'
    assert all(rows[key] == value for key, value in originals.items()), 'Existing run changed'
    index = json.loads((state / 'sessions.json').read_text(encoding='utf-8'))
    assert index.get('active_session_id') == original_index.get('active_session_id')
    for key, value in original_index.get('sessions', {}).items():
        assert index['sessions'][key] == value, 'Existing index changed: ' + key
    for key, value in original_index.items():
        if key != 'sessions':
            assert index.get(key) == value


def verify_runtime():
    assert command('docker', 'inspect', BACKEND, '--format', '{{.Config.Image}}') == IMAGE
    for version in VERSIONS:
        run = json.loads((ARCHIVE / version / 'run.json').read_text(encoding='utf-8'))
        expression = ("import json,urllib.request,io;from openpyxl import load_workbook;"
                      f"u='http://127.0.0.1:9014/api/simulations/{run['id']}';"
                      f"r=json.load(urllib.request.urlopen(u,timeout=10));assert len(r['messages'])==150 and r['status']=={run['status']!r};")
        if version == SELECTED:
            expression += "b=urllib.request.urlopen(u+'/export/xlsx',timeout=10).read();"
        else:
            expression += "from app.core import export_xlsx;b=export_xlsx(r);"
        expression += (
                      "s=load_workbook(io.BytesIO(b)).active;assert (s.max_row,s.max_column)==(151,9)")
        command('docker', 'exec', BACKEND, 'python', '-c', expression)


def link_to(target):
    temporary = Path('/opt/vimax/current.safe-' + uuid.uuid4().hex)
    temporary.symlink_to(target)
    os.replace(temporary, '/opt/vimax/current')


def restore(snapshot_path, old):
    # Caller guarantees the workbench is still stopped. No post-reopen DB rollback.
    command('docker', 'stop', '--time', '30', BACKEND)
    path = STATE / 'dialogue/app.sqlite3'
    temporary = path.with_name('app.sqlite3.restore-' + uuid.uuid4().hex)
    shutil.copy2(snapshot_path / 'dialogue/app.sqlite3', temporary)
    for suffix in ('-wal', '-shm'):
        try:
            path.with_name(path.name + suffix).unlink()
        except FileNotFoundError:
            pass
    os.replace(temporary, path)
    temporary = STATE / ('sessions.restore-' + uuid.uuid4().hex)
    shutil.copy2(snapshot_path / 'sessions.json', temporary)
    os.replace(temporary, STATE / 'sessions.json')
    link_to(old)
    compose(old, 'up', '-d', '--no-deps', 'backend')
    health()
    assert command('docker', 'inspect', BACKEND, '--format', '{{.Config.Image}}') == 'vimax-dialogue-backend:' + Path(old).name
    assert database_rows(STATE) == database_rows(snapshot_path), 'Rollback DB verification failed'
    assert (STATE / 'sessions.json').read_bytes() == (snapshot_path / 'sessions.json').read_bytes()
    assert Path('/opt/vimax/current').resolve() == Path(old)


def register_batch(manifest):
    import fcntl
    path = STATE / 'dialogue-versions/batches.json'
    with (path.parent / 'batches.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        record = {'batch': BATCH, 'sha256': manifest, 'release': RELEASE,
                  'previous_release': OLD, 'versions': list(VERSIONS), 'selected': SELECTED}
        assert BATCH not in data or data[BATCH] == record, 'Batch manifest collision'
        data[BATCH] = record
        atomic_json(path, data)


def remote(mode):
    import fcntl
    with Path('/opt/vimax/natural-publish.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = verify_archive(ARCHIVE)
        image_id = command('docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}')
        web_image_id = command('docker', 'image', 'inspect', WEB_IMAGE, '--format', '{{.Id}}')
        assert Path(RELEASE + '/release.env').is_file()
        assert Path('/opt/vimax/current').resolve() == Path(OLD), 'Unexpected current release; refusing'
        if mode == 'candidate':
            candidate = Path('/opt/vimax/candidates') / (BATCH + '-' + uuid.uuid4().hex)
            snapshot(STATE, candidate / 'state')
            # Snapshot data itself is the baseline, rather than a racing earlier read.
            originals = database_rows(candidate / 'state')
            original_index = json.loads((candidate / 'state/sessions.json').read_text(encoding='utf-8'))
            import_runs(candidate / 'state', candidate / 'working')
            import_runs(candidate / 'state', candidate / 'working')  # Real retry exercise.
            verify_state(candidate / 'state', originals, original_index)
            atomic_json(ARCHIVE.parent / (BATCH + '.candidate-passed.json'),
                        {'sha256': manifest, 'candidate': str(candidate),
                         'selected': SELECTED, 'versions': list(VERSIONS),
                         'image_id': image_id, 'web_image_id': web_image_id,
                         'previous_release': OLD})
            print('Candidate and identical retry passed: ' + str(candidate), flush=True)
            return
        receipt = json.loads((ARCHIVE.parent / (BATCH + '.candidate-passed.json')).read_text(encoding='utf-8'))
        assert receipt['sha256'] == manifest, 'This exact archive has not passed candidate verification'
        assert receipt['selected'] == SELECTED and receipt['versions'] == list(VERSIONS)
        assert receipt['image_id'] == image_id, 'Backend image changed since candidate verification'
        assert receipt['web_image_id'] == web_image_id, 'Workbench image changed since candidate verification'
        assert receipt.get('previous_release', '/opt/vimax/releases/20261014-a335ea6') == OLD, 'Previous release differs from candidate verification'
        assert is_running(WORKBENCH) and is_running(BACKEND), 'Expected live ViMax containers'
        assert command('docker', 'inspect', BACKEND, '--format', '{{.Config.Image}}') == 'vimax-dialogue-backend:' + Path(OLD).name
        assert command('docker', 'inspect', WORKBENCH, '--format', '{{.Config.Image}}') == 'vimax-dialogue-web:' + Path(OLD).name
        backup = Path('/opt/backups') / ('vimax-natural-' + BATCH + '-' + uuid.uuid4().hex)
        working = STATE / 'dialogue-imports' / BATCH
        gate_closed = False
        backend_stopped = False
        snapshot_ready = False
        reopened = False
        try:
            # All commands affect only this Compose project's two named containers.
            gate_closed = True
            command('docker', 'stop', '--time', '30', WORKBENCH)
            assert not is_running(WORKBENCH)
            runs = database_rows(STATE)
            assert not any(json.loads(value)['status'] == 'running' for value in runs.values()), 'Running generation; publication deferred without interrupting it'
            command('docker', 'stop', '--time', '30', BACKEND)
            backend_stopped = True
            snapshot(STATE, backup)
            (backup / 'previous-release.txt').write_text(OLD)
            snapshot_ready = True
            originals = database_rows(backup)
            original_index = json.loads((backup / 'sessions.json').read_text(encoding='utf-8'))
            import_runs(STATE, working)
            verify_state(STATE, originals, original_index)
            compose(RELEASE, 'up', '-d', '--no-deps', 'backend')
            health()
            command('docker', 'exec', BACKEND, 'python', '-c',
                    "from pathlib import Path;Path('/app/.working_dir').mkdir(parents=True,exist_ok=True)")
            command('docker', 'cp', working.as_posix() + '/.', BACKEND + ':/app/.working_dir/')
            verify_runtime()
            verify_state(STATE, originals, original_index)
            link_to(RELEASE)
            atomic_json(backup / 'publication.json', {'batch': BATCH, 'stage': 'verified', 'archive': ARCHIVE.as_posix()})
            # A timeout from Compose has ambiguous success. From this point, never
            # auto-restore data; the entry may already have accepted a new user request.
            reopened = True
            compose(RELEASE, 'up', '-d', '--no-deps', 'workbench')
            assert is_running(WORKBENCH)
            assert command('docker', 'inspect', WORKBENCH, '--format', '{{.Config.Image}}') == WEB_IMAGE
            register_batch(manifest)  # Lookup manifest is the last publication step.
            atomic_json(backup / 'publication.json', {'batch': BATCH, 'stage': 'entry-restored', 'archive': ARCHIVE.as_posix()})
            print('Published; original records unchanged; backup=' + str(backup), flush=True)
        except BaseException as failure:
            if reopened:
                raise RuntimeError('Entry restoration attempted; NO automatic DB rollback. Verify/start workbench, preserve new writes. Backup=' + str(backup)) from failure
            try:
                if snapshot_ready:
                    restore(backup, OLD)
                elif backend_stopped:
                    compose(OLD, 'up', '-d', '--no-deps', 'backend')
                    health()
                if gate_closed:
                    compose(OLD, 'up', '-d', '--no-deps', 'workbench')
                    assert is_running(WORKBENCH)
                    assert command('docker', 'inspect', WORKBENCH, '--format', '{{.Config.Image}}') == 'vimax-dialogue-web:' + Path(OLD).name
            except BaseException as recovery:
                raise RuntimeError('Recovery failed; ViMax entry remains closed; manual recovery required. Backup=' + str(backup) + '; ' + str(recovery)) from failure
            raise RuntimeError('Publication cancelled; previous service verified/restored. ' + str(failure)) from failure


def publishable_artifact(path, directory):
    relative = path.relative_to(directory)
    return (path.is_file() and not any(part.startswith('.') or part == '__pycache__' for part in relative.parts)
            and path.suffix not in {'.pyc', '.lock', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'}
            and '.tmp-' not in path.name and not path.name.endswith('.tmp'))


def upload_and_run(mode):
    import paramiko
    root = Path(__file__).resolve().parents[2]
    output = root / 'outputs/自然群聊-20261015'
    files = {}
    for version in VERSIONS:
        run = json.loads((output / version / 'run.json').read_text(encoding='utf-8'))
        assert len(run['messages']) == 150
        if version == SELECTED:
            assert run['status'] == 'completed' and not run['errors'] and not run['quality_review']
        for path in (output / version).rglob('*'):
            if publishable_artifact(path, output / version):
                files[path.relative_to(output).as_posix()] = path
    files['versions.json'] = output / 'versions.json'
    if (output / 'evolution-corrections.json').is_file():
        files['evolution-corrections.json'] = output / 'evolution-corrections.json'
    files['import-run.py'] = root / '.vimax/deploy/import-natural-dialogue.py'
    files['publish.py'] = Path(__file__).resolve()
    manifest = {name: digest(path) for name, path in files.items()}
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('217.194.133.194', port=11476, username='root',
                password=os.environ['VIMAX_TASK_SSH_PASSWORD'], timeout=20)
    try:
        sftp = ssh.open_sftp()
        archive = ARCHIVE.as_posix()
        assert archive.startswith('/') and '\\' not in archive, 'Remote archive must be an absolute POSIX path'
        try:
            with sftp.file(archive + '/sha256.json') as stream:
                existing = json.loads(stream.read().decode())
        except FileNotFoundError:
            existing = None
        if existing is not None:
            assert existing == manifest, 'Immutable archive already exists with different content; use a new batch'
        else:
            staging = archive + '.upload-' + uuid.uuid4().hex
            sftp.mkdir(staging, mode=0o700)
            directories = {staging}
            for name, path in files.items():
                remote_path = staging + '/' + name
                parent = PurePosixPath(name).parent.as_posix()
                cursor = staging
                if parent != '.':
                    for part in parent.split('/'):
                        cursor += '/' + part
                        if cursor not in directories:
                            sftp.mkdir(cursor, mode=0o700)
                            directories.add(cursor)
                sftp.put(str(path), remote_path)
                sftp.chmod(remote_path, 0o600)
            with sftp.file(staging + '/sha256.json', 'w') as stream:
                stream.write(json.dumps(manifest, sort_keys=True))
            sftp.chmod(staging + '/sha256.json', 0o600)
            # Server validates every file before publishing this directory atomically.
            verify = ("import hashlib,json;from pathlib import Path;"
                      f"p=Path({staging!r});m=json.loads((p/'sha256.json').read_text(encoding='utf-8'));"
                      "a={x.relative_to(p).as_posix():hashlib.sha256(x.read_bytes()).hexdigest() for x in p.rglob('*') if x.is_file() and x.name!='sha256.json'};assert a==m")
            import shlex
            _, out, err = ssh.exec_command('python3 -c ' + shlex.quote(verify), timeout=60)
            assert out.channel.recv_exit_status() == 0, err.read().decode()
            sftp.rename(staging, archive)
        sftp.close()
        import shlex
        remote_command = ('python3 ' + shlex.quote(archive + '/publish.py') + ' --remote ' + mode
                          + ' --revision ' + BATCH.rsplit('-', 1)[1] + ' --selected ' + SELECTED
                          + ' --versions ' + ','.join(VERSIONS) + ' --previous ' + Path(OLD).name)
        _, out, err = ssh.exec_command(remote_command, timeout=900)
        out.channel.set_combine_stderr(True)
        for line in out:
            print(line, end='', flush=True)
        assert out.channel.recv_exit_status() == 0, 'Remote operation failed; inspect output and the printed backup path'
    finally:
        ssh.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['candidate', 'publish'])
    parser.add_argument('--revision', required=True)
    parser.add_argument('--selected', required=True)
    parser.add_argument('--versions', default='V01,V02,V03')
    parser.add_argument('--previous', default='20261014-a335ea6')
    parser.add_argument('--remote', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure(args.revision, args.selected, args.versions.split(','), args.previous)
    if args.remote:
        remote(args.mode)
    else:
        upload_and_run(args.mode)
