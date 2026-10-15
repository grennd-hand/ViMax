"""Frozen independent-series publisher; prepare is local, other modes are explicit SSH.

The frozen natural publisher owns backup/pins/lock/running-task/rollback rules.
Only artifact transfer, sequential bulk imports and independent verification
are hooked here. A candidate's two import passes share one Docker container.
"""
import argparse
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import uuid

sys.dont_write_bytecode = True
SERIES = '独立随机-20261015'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def validate_runtime_corpus(payload):
    corpus = json.loads(payload)
    assert corpus.get('schema_version') == 1 and corpus.get('sources') and corpus.get('conversations'), 'Processed runtime corpus required'
    sources = {source['source_id'] for source in corpus['sources']}
    assert len(sources) == len(corpus['sources'])
    assert all(source.get('url') and source.get('license') for source in corpus['sources'])
    seen = set()
    for item in corpus['conversations']:
        texts = [turn['text'] for turn in item['turns']]
        assert len(texts) >= 2 and all(isinstance(text, str) and text for text in texts)
        digest = sha(canonical(texts))
        assert item['id'] == item['content_sha256'] == digest and digest not in seen
        assert item.get('attributions') and all(ref['source_id'] in sources for ref in item['attributions'])
        seen.add(digest)
    return corpus


def install_runtime_corpus(state, payload):
    validate_runtime_corpus(payload)
    target = state / 'dialogue/dialogue-corpus/lccc.json'
    assert not any(path.is_symlink() for path in (state, state / 'dialogue', target.parent, target)), 'Runtime corpus symlink rejected'
    if target.exists():
        assert target.read_bytes() == payload, 'Existing runtime corpus differs; refusing overwrite'
        return False
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name('.lccc-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream: stream.write(payload)
        temporary.chmod(0o600)
        # Same-filesystem hard link installs atomically with no overwrite race.
        os.link(str(temporary), str(target))
    finally:
        if temporary.exists(): temporary.unlink()
    return True


def safe_name(name):
    assert isinstance(name, str) and name and '\\' not in name and ':' not in name, 'Unsafe archive path'
    path = PurePosixPath(name)
    assert not path.is_absolute() and str(path) == name, 'Noncanonical archive path'
    assert all(part not in ('.', '..') and not part.startswith('.') for part in path.parts), 'Private/escaping archive path'
    assert not any(part in ('__pycache__', 'node_modules') for part in path.parts), 'Runtime archive path'
    assert not path.name.endswith(('.pyc', '.lock', '.sqlite', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.db', '.tmp')), 'Runtime archive file'
    assert path.name not in ('runtime-model.yaml', 'agent.local.yaml', 'release.env', 'access.env'), 'Private configuration'
    return path


def verify_frozen(directory):
    directory = Path(directory)
    assert not directory.is_symlink(), 'Archive cannot be a symlink'
    assert not (directory / 'sha256.json').is_symlink(), 'Manifest symlink rejected'
    manifest = json.loads((directory / 'sha256.json').read_text(encoding='utf-8'))
    assert isinstance(manifest, dict) and manifest
    for name, digest in manifest.items():
        safe_name(name)
        assert name != 'sha256.json' and re.fullmatch('[0-9a-f]{64}', digest), 'Invalid manifest'
    actual = {}
    for path in directory.rglob('*'):
        assert not path.is_symlink(), 'Archive symlink rejected'
        if path.is_file() and path != directory / 'sha256.json':
            name = path.relative_to(directory).as_posix()
            safe_name(name)
            actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == manifest, 'Exact archive file set/SHA mismatch'
    return manifest


def extract_archive(bundle_path, destination, manifest_sha256, bundle_sha256):
    """No extractall: validate every member before writing any destination file."""
    bundle_path, destination = Path(bundle_path), Path(destination)
    assert hashlib.sha256(bundle_path.read_bytes()).hexdigest() == bundle_sha256, 'Bundle SHA mismatch'
    with tarfile.open(str(bundle_path), 'r:gz') as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        assert len(names) == len(set(names)), 'Duplicate archive member'
        assert len(names) == len({name.casefold() for name in names}), 'Case-colliding archive member'
        assert sum(member.size for member in members) <= 1024 * 1024 * 1024, 'Archive size exceeds limit'
        for member in members:
            safe_name(member.name)
            assert member.isfile() and member.size <= 128 * 1024 * 1024, 'Links/devices/directories/oversized member rejected'
        assert 'sha256.json' in names
        manifest = json.load(bundle.extractfile('sha256.json'))
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        assert hashlib.sha256(raw).hexdigest() == manifest_sha256, 'Frozen manifest SHA mismatch'
        assert set(names) == set(manifest) | {'sha256.json'}, 'Archive member set differs from manifest'
        for name, digest in manifest.items():
            safe_name(name)
            assert re.fullmatch('[0-9a-f]{64}', digest)
            assert hashlib.sha256(bundle.extractfile(name).read()).hexdigest() == digest, 'Member SHA mismatch: ' + name
        if destination.exists():
            assert verify_frozen(destination) == manifest, 'Immutable archive conflict'
            return 'existing_identical'
        assert not destination.is_symlink(), 'Archive path symlink rejected'
        staging = destination.with_name(destination.name + '.staging-' + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        for member in members:
            path = staging.joinpath(*PurePosixPath(member.name).parts)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open('xb') as stream:
                stream.write(bundle.extractfile(member).read())
            path.chmod(0o600)
        verify_frozen(staging)
        assert not destination.exists(), 'Archive appeared during staging; refusing overwrite'
        os.rename(str(staging), str(destination))
    return 'installed'


def arguments(target_release, previous, versions, selected):
    assert re.fullmatch('[0-9]{8}-[0-9a-f]{7}', target_release), 'Explicit YYYYMMDD-7hex target release required'
    assert re.fullmatch('[0-9]{8}-[0-9a-f]{7}', previous) and previous != target_release
    assert versions and len(versions) == len(set(versions)) and (selected is None or selected in versions)
    assert all(re.fullmatch(r'V[0-9]{2}(?:-R[0-9]{3,})?', version) and 1 <= int(version[1:3]) <= 40 for version in versions)
    return {'series': SERIES, 'target_release': target_release, 'previous': previous,
            'versions': list(versions), 'selected': selected}


def collect_files(root, plan):
    output, scripts = root / 'outputs' / SERIES, root / '.vimax/deploy'
    assert not output.is_symlink(), 'Source root symlink rejected'
    importer = load_module('independent_importer_preflight', scripts / 'import-natural-dialogue.py')
    runtime_path = output / 'lccc.corpus.json'
    runtime_payload = runtime_path.read_bytes()
    runtime_corpus = validate_runtime_corpus(runtime_payload)
    corpus_sources = {source['source_id']: source for source in runtime_corpus['sources']}
    files, ids, worlds = {}, set(), set()
    registry = json.loads((output / 'versions.json').read_bytes())
    for version in plan['versions']:
        folder = output / version
        assert not folder.is_symlink(), 'Version root symlink rejected'
        run = json.loads((folder / 'run.json').read_bytes())
        importer.independent_evidence(run, version)
        assert run['version_record'].get('corpus', {}).get('file_sha256') == sha(runtime_payload), 'Run used a different runtime corpus'
        assert all(corpus_sources.get(source.get('source_id')) == source for source in run['corpus_reference']['sources']), 'Run corpus source attribution differs'
        assert len(run['messages']) == 150 and [m['seq'] for m in run['messages']] == list(range(1, 151))
        assert run['status'] in ('completed', 'failed', 'paused') and run.get('candidate_versions')
        assert run['id'] not in ids and run['config']['story_world'] not in worlds, 'Independent run/world reused'
        ids.add(run['id']); worlds.add(run['config']['story_world'])
        registrations = [entry for entry in registry if entry['version'] == version and entry['run_id'] == run['id']]
        assert len(registrations) == 1 and registrations[0].get('publication_eligible') is True, 'Unregistered or not publication-eligible run'
        if version == plan['selected']:
            assert run['status'] == 'completed' and not run['errors'] and not run['quality_review']
        hashes = json.loads((folder / 'hashes.json').read_bytes())
        for name, expected in hashes.items():
            safe_name(name)
            path = folder.joinpath(*PurePosixPath(name).parts)
            assert not path.is_symlink() and sha(path.read_bytes()) == expected, 'Source checksum mismatch: ' + version + '/' + name
        for path in folder.rglob('*'):
            relative = path.relative_to(output).as_posix()
            try:
                safe_name(relative)
            except AssertionError:
                continue
            assert not path.is_symlink(), 'Source symlink rejected'
            if path.is_file(): files[relative] = path
    corpus = output / '语料来源'
    assert corpus.is_dir() and not corpus.is_symlink(), 'Public corpus provenance/license/original sample directory required'
    for path in corpus.rglob('*'):
        name = path.relative_to(output).as_posix()
        try:
            safe_name(name)
        except AssertionError:
            continue
        assert not path.is_symlink(), 'Corpus symlink rejected'
        if path.is_file(): files[name] = path
    assert any(name.endswith('metadata.json') for name in files if name.startswith('语料来源/'))
    assert any('LICENSE' in name for name in files if name.startswith('语料来源/'))
    assert any(name.endswith(('.jsonl.gz', '/conversations.json')) for name in files if name.startswith('语料来源/'))
    files.update({'versions.json': output / 'versions.json', 'publish.py': Path(__file__),
                  'publish-natural-base.py': scripts / 'publish-natural-safe.py',
                  'import-run.py': scripts / 'import-natural-dialogue.py',
                  'corpus/lccc.json': runtime_path})
    return files


def prepare(root, target_release, previous, versions, selected):
    plan = arguments(target_release, previous, versions, selected)
    files = collect_files(root, plan)
    payloads = {name: path.read_bytes() for name, path in files.items()}
    plan['runtime_corpus_sha256'] = sha(payloads['corpus/lccc.json'])
    plan['source_hashes'] = {name: sha(payloads[name]) for name in ('publish.py', 'publish-natural-base.py', 'import-run.py')}
    payloads['publication-plan.json'] = canonical(plan)
    manifest = {name: sha(value) for name, value in payloads.items()}
    payloads['sha256.json'] = canonical(manifest)
    directory = root / '.vimax/deploy/independent-publication' / target_release
    directory.mkdir(parents=True, exist_ok=False)
    bundle = directory / 'archive.tar.gz'
    with tarfile.open(str(bundle), 'w:gz') as archive:
        for name, value in payloads.items():
            safe_name(name)
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(value), 0o600, 0
            archive.addfile(member, io.BytesIO(value))
    receipt = {'plan': plan, 'manifest': manifest, 'manifest_sha256': sha(canonical(manifest)),
               'bundle_sha256': sha(bundle.read_bytes()), 'file_count': len(payloads)}
    with (directory / 'prepared.json').open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'prepared': str(bundle), 'files': len(payloads), 'bundle_sha256': receipt['bundle_sha256']}, ensure_ascii=False))
    return directory


def batch_import(archive, root, versions, selected, repeats):
    assert repeats in (1, 2)
    assert versions and len(versions) == len(set(versions)) and (selected is None or selected in versions)
    assert all(re.fullmatch(r'V[0-9]{2}(?:-R[0-9]{3,})?', version) and 1 <= int(version[1:3]) <= 40 for version in versions)
    importer = load_module('frozen_bulk_importer', archive / 'import-run.py')
    saved = sys.argv[:]
    try:
        for attempt in range(repeats):
            for version in versions:
                sys.argv = ['import-run.py', '--root', str(root), '--db', str(root / '.vimax/dialogue/app.sqlite3'),
                            '--run', str(archive / version / 'run.json'), '--template', str(archive / version / 'template.json'),
                            '--version', version] + (['--selected'] if version == selected else [])
                print('Sequential import pass=' + str(attempt + 1) + ' ' + version, flush=True)
                importer.main()
    finally:
        sys.argv = saved


def configure_base(plan, mode, directory):
    base = load_module('frozen_independent_transaction', directory / 'publish-natural-base.py')
    original_verify, original_restore, original_runtime = base.verify_state, base.restore, base.verify_runtime
    # The legacy constructor requires a value; no operation runs until its
    # SELECTED is explicitly replaced with the optional user choice below.
    base.configure(plan['target_release'][-7:], plan['selected'] or plan['versions'][0], plan['versions'], plan['previous'])
    base.SELECTED = plan['selected']
    base.BATCH = plan['target_release']; base.RELEASE = '/opt/vimax/releases/' + base.BATCH
    base.IMAGE = 'vimax-dialogue-backend:' + base.BATCH; base.WEB_IMAGE = 'vimax-dialogue-web:' + base.BATCH
    base.ARCHIVE = base.STATE / 'dialogue-versions' / base.BATCH
    calls, created_corpora = [], []
    def runtime_payload():
        payload = (base.ARCHIVE / 'corpus/lccc.json').read_bytes()
        if plan.get('runtime_corpus_sha256'):
            assert sha(payload) == plan['runtime_corpus_sha256']
        return payload
    def bulk(state, working):
        if mode == 'candidate' and calls: return  # The original transaction calls import_runs twice.
        calls.append(True)
        if install_runtime_corpus(state, runtime_payload()):
            created_corpora.append(state / 'dialogue/dialogue-corpus/lccc.json')
        working.mkdir(parents=True, exist_ok=True)
        command = ['docker', 'run', '--rm', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                   '-v', state.as_posix() + ':/app/.vimax', '-v', working.as_posix() + ':/app/.working_dir',
                   '-v', base.ARCHIVE.as_posix() + ':/verification:ro', base.IMAGE,
                   'python', '/verification/publish.py', 'batch-import', '--versions', ','.join(plan['versions']),
                   '--repeats', '2' if mode == 'candidate' else '1']
        if plan['selected'] is not None: command += ['--selected', plan['selected']]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=900)
        if result.stdout: print(result.stdout, end='', flush=True)
        assert result.returncode == 0, 'Bulk importer failed: ' + result.stderr[-3000:]
    def verify(state, originals, index):
        original_verify(state, originals, index)
        assert (state / 'dialogue/dialogue-corpus/lccc.json').read_bytes() == runtime_payload()
        rows = base.database_rows(state)
        for version in plan['versions']:
            source = json.loads((base.ARCHIVE / version / 'run.json').read_text(encoding='utf-8'))
            actual = json.loads(rows[source['id']])
            for field in ('creative_variation', 'corpus_reference', 'independence'):
                assert actual.get(field) == source.get(field), 'Independent evidence changed: ' + field
            publication = actual['publication']
            assert publication['series'] == SERIES and publication['parent_run'] is None and publication['read_only'] is True
            assert publication['source_run_sha256'] == base.digest(base.ARCHIVE / version / 'run.json')
            assert publication['selected'] == publication['user_selected'] == (version == plan['selected'])
            review = json.loads((base.ARCHIVE / version / 'review-index.json').read_text(encoding='utf-8'))
            assert publication['human_verdict'] == review['verdict']
            assert publication['style_label'] == source['creative_variation']['style_label']
            state_label = ('deferred' if source.get('program_review_deferred') is True
                           else 'reviewed' if source.get('program_review_history') else 'not_completed')
            assert publication['program_review_state'] == state_label
    def restore(snapshot_path, old):
        original_restore(snapshot_path, old)
        target = base.STATE / 'dialogue/dialogue-corpus/lccc.json'
        if target in created_corpora:
            assert not target.is_symlink() and target.read_bytes() == runtime_payload(), 'Changed new corpus retained for manual recovery'
            target.unlink()  # Only this transaction's new file, before the outer transaction reopens entry.
    def runtime():
        original_runtime()
        corpus = validate_runtime_corpus(runtime_payload())
        expression = ("import hashlib,json,urllib.request;from pathlib import Path;"
                      "p=Path('/app/.vimax/dialogue/dialogue-corpus/lccc.json');"
                      "assert hashlib.sha256(p.read_bytes()).hexdigest()==" + repr(sha(runtime_payload())) + ';'
                      "r=json.load(urllib.request.urlopen('http://127.0.0.1:9014/api/dialogue-corpus',timeout=10));"
                      "assert r['available'] is True and r['conversations']==" + repr(len(corpus['conversations'])) + ';'
                      "assert r['sources']==" + repr(corpus['sources']))
        base.command('docker', 'exec', base.BACKEND, 'python', '-c', expression)
    base.import_runs, base.verify_state, base.verify_archive = bulk, verify, verify_frozen
    base.restore, base.verify_runtime = restore, runtime
    return base


def ssh_run(client, command, password):
    _, output, _ = client.exec_command(command, timeout=1800)
    output.channel.set_combine_stderr(True)
    for line in output: print(line.replace(password, '[redacted]'), end='', flush=True)
    assert output.channel.recv_exit_status() == 0, 'Remote operation failed; preserve staged artifacts and inspect output'


def upload(mode, root, plan, host, port, user):
    directory = root / '.vimax/deploy/independent-publication' / plan['target_release']
    receipt = json.loads((directory / 'prepared.json').read_bytes())
    assert all(receipt['plan'][key] == value for key, value in plan.items()), 'Prepared plan differs'
    assert receipt['plan']['source_hashes']['publish.py'] == sha(Path(__file__).read_bytes()), 'Publisher changed after preparation'
    bundle = directory / 'archive.tar.gz'
    assert sha(bundle.read_bytes()) == receipt['bundle_sha256']
    password = os.environ.get('VIMAX_TASK_SSH_PASSWORD')
    assert password and host, 'Set SSH host and VIMAX_TASK_SSH_PASSWORD environment'
    import paramiko
    client = paramiko.SSHClient(); client.load_system_host_keys(); client.set_missing_host_key_policy(paramiko.RejectPolicy())
    archive = '/opt/vimax/shared/state/dialogue-versions/' + plan['target_release']
    try:
        client.connect(host, port=port, username=user, password=password, timeout=20, allow_agent=False, look_for_keys=False)
        guard = 'test "$(readlink -f /opt/vimax/current)" = ' + shlex.quote('/opt/vimax/releases/' + plan['previous'])
        ssh_run(client, guard, password)
        with client.open_sftp() as sftp:
            try:
                with sftp.file(archive + '/sha256.json', 'rb') as stream: existing = json.loads(stream.read())
            except FileNotFoundError: existing = None
            if existing is not None:
                assert existing == receipt['manifest'], 'Immutable archive differs; use a new target release'
            else:
                remote_bundle = archive + '.upload-' + uuid.uuid4().hex + '.tar.gz'
                sftp.put(str(bundle), remote_bundle); sftp.chmod(remote_bundle, 0o600)
                bootstrap = ('import hashlib,json,os,re,tarfile,uuid,fcntl;from pathlib import Path,PurePosixPath\n'
                             + inspect.getsource(safe_name) + '\n' + inspect.getsource(verify_frozen) + '\n' + inspect.getsource(extract_archive)
                             + "\nwith open('/opt/vimax/natural-publish.lock','a+') as lock:\n"
                             + '    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                             + '    print(extract_archive(' + repr(remote_bundle) + ',' + repr(archive) + ','
                             + repr(receipt['manifest_sha256']) + ',' + repr(receipt['bundle_sha256']) + '))\n')
                ssh_run(client, 'python3 -c ' + shlex.quote(bootstrap), password)
        if mode != 'prestage':
            command = ['python3', archive + '/publish.py', mode, '--remote', '--target-release', plan['target_release'],
                       '--previous', plan['previous'], '--versions', ','.join(plan['versions'])]
            if plan['selected'] is not None: command += ['--selected', plan['selected']]
            ssh_run(client, ' '.join(shlex.quote(part) for part in command), password)
        else:
            # Existing archives also receive a real full-file rehash, never only a manifest comparison.
            command = ['python3', archive + '/publish.py', 'verify', '--remote']
            ssh_run(client, ' '.join(shlex.quote(part) for part in command), password)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'prestage', 'candidate', 'publish', 'verify', 'batch-import'))
    parser.add_argument('--target-release'); parser.add_argument('--previous')
    parser.add_argument('--versions'); parser.add_argument('--selected')
    parser.add_argument('--remote', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--host', default=os.environ.get('VIMAX_TASK_SSH_HOST'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('VIMAX_TASK_SSH_PORT', '22')))
    parser.add_argument('--user', default=os.environ.get('VIMAX_TASK_SSH_USER', 'root'))
    args = parser.parse_args(); here = Path(__file__).resolve().parent
    if args.mode == 'batch-import':
        batch_import(here, Path('/app'), args.versions.split(','), args.selected, args.repeats); return
    if args.mode == 'verify' and args.remote:
        verify_frozen(here); print('Exact frozen archive verified'); return
    plan = arguments(args.target_release or '', args.previous or '', (args.versions or '').split(','), args.selected)
    if args.remote:
        assert args.mode in ('candidate', 'publish')
        manifest = verify_frozen(here)
        frozen = json.loads((here / 'publication-plan.json').read_bytes())
        assert all(frozen.get(key) == value for key, value in plan.items()), 'Frozen remote plan differs'
        for name, digest in frozen['source_hashes'].items(): assert manifest[name] == digest
        configure_base(frozen, args.mode, here).remote(args.mode)
    else:
        root = here.parents[1]
        if args.mode == 'prepare': prepare(root, **plan_without_series(plan))
        else:
            assert args.mode in ('prestage', 'candidate', 'publish')
            upload(args.mode, root, plan, args.host, args.port, args.user)


def plan_without_series(plan):
    return {key: value for key, value in plan.items() if key != 'series'}


if __name__ == '__main__': main()
