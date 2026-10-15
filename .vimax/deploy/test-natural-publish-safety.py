"""Local-only fault injection. No SSH, Docker, model calls, or secret reads."""
import contextlib
import ast
import hashlib
from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path, PureWindowsPath
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = load('safe_publisher', HERE / 'publish-natural-safe.py')
importer = load('safe_importer', HERE / 'import-natural-dialogue.py')


def run(version):
    identifier = 'a' * 30 + version[1:]
    return {'id': identifier, 'vimax_project_id': 'dialogue-' + identifier, 'template_id': 'template',
            'status': 'failed' if version == 'V01' else 'completed',
            'messages': [{'seq': i, 'sender': '坐席', 'content': str(i)} for i in range(1, 151)],
            'errors': ['needs adjustment'] if version == 'V01' else [], 'quality_review': [],
            'created_at': '2026-10-15T00:00:00',
            'config': {'target_messages': 150, 'dialogue_flow_version': 3,
                       'dialogue_model': 'gpt-6-sol', 'reasoning_effort': 'high'}}


def fixture(root):
    (root / '.vimax/dialogue').mkdir(parents=True)
    old = {'id': 'old', 'status': 'paused', 'messages': []}
    with closing(sqlite3.connect(root / '.vimax/dialogue/app.sqlite3')) as db:
        db.execute('create table sessions(id text primary key, template_id text, run_json text, created_at text)')
        db.execute('create table templates(id text primary key, source_json text, analysis_json text)')
        db.execute('insert into sessions values(?,?,?,?)', ('old', 'template', json.dumps(old), 'today'))
        db.execute('insert into templates values(?,?,?)', ('template', '{}', '{"primary_goal":"test"}'))
        db.commit()
    (root / '.vimax/sessions.json').write_text(json.dumps({'active_session_id': 'old-project',
        'sessions': {'old-project': {'session_id': 'old-project', 'project_name': 'keep me'}}}))


class ImportSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.run_path = self.root / 'run.json'
        self.run_path.write_text(json.dumps(run('V02')))
        self.template = self.root / 'template.json'
        self.template.write_text('{"source":{},"analysis":{"primary_goal":"test"}}')
        self.argv = ['import', '--root', str(self.root), '--db', str(self.root / '.vimax/dialogue/app.sqlite3'),
                     '--run', str(self.run_path), '--template', str(self.template), '--version', 'V02', '--selected']
        self.core = types.ModuleType('app.core')
        self.core.export_xlsx = lambda value: b'prepared-export'
        engine = types.ModuleType('app.vimax_engine')
        engine.contract_errors = lambda source, value: []
        self.engine = engine
        memory = types.ModuleType('app.memory_store')
        memory.bind_characters = lambda db, value: None
        memory.sync_accepted_memory = lambda db, value: None
        self.modules = patch.dict(sys.modules, {'app': types.ModuleType('app'), 'app.core': self.core,
                                               'app.vimax_engine': engine, 'app.memory_store': memory})
        self.modules.start()
        self.old_index = (self.root / '.vimax/sessions.json').read_bytes()

    def tearDown(self):
        self.modules.stop()
        self.temp.cleanup()

    def invoke(self):
        with patch.object(sys, 'argv', self.argv), contextlib.redirect_stdout(io.StringIO()):
            importer.main()

    def snapshot_fixture(self):
        value = run('V03')
        value['config']['dialogue_flow_version'] = 4
        value['messages'][0].update(type='图片+文本', image_code='SCREENSHOT_001', image_mode='待补素材')
        wording = [{key: m.get(key) for key in importer.MESSAGE_FIELDS} for m in value['messages']]
        fingerprint = hashlib.sha256(importer.canonical(wording).encode()).hexdigest()
        folder_name = 'R000-' + fingerprint[:8]
        record = {'number': 0, 'version': 'R000', 'hash': fingerprint,
                  'reason': '完整初稿', 'capture_status': 'running', 'code': '2965645',
                  'version_record': {'code': '2965645'},
                  'path': 'versions/' + value['id'] + '/' + folder_name}
        value['candidate_versions'] = [record]
        snapshot = json.loads(json.dumps(value))
        snapshot['status'] = 'running'
        payloads = {'run.json': json.dumps(snapshot).encode(), 'messages.md': b'draft markdown',
                    'messages.xlsx': b'fixture spreadsheet bytes'}
        manifest = {'record': record, 'hash_fields': list(importer.MESSAGE_FIELDS), 'message_count': 150,
                    'files': {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}}
        payloads['manifest.json'] = json.dumps(manifest).encode()
        source = self.root / 'snapshots' / folder_name
        source.mkdir(parents=True)
        for name, payload in payloads.items():
            (source / name).write_bytes(payload)
        self.run_path.write_text(json.dumps(value))
        self.argv[self.argv.index('--version') + 1] = 'V03'
        target = self.root / '.vimax/dialogue' / record['path']
        return value, source, target, payloads

    def test_identical_retry_keeps_old_records_and_active_project(self):
        self.invoke()
        self.invoke()
        rows = publisher.database_rows(self.root / '.vimax')
        self.assertEqual(len(rows), 2)
        data = json.loads((self.root / '.vimax/sessions.json').read_text(encoding='utf-8'))
        self.assertEqual(data['active_session_id'], 'old-project')
        self.assertEqual(data['sessions']['old-project']['project_name'], 'keep me')

    def test_index_failure_after_database_commit_is_repairable(self):
        from agent_runtime.session_index import SessionIndex
        with patch.object(SessionIndex, 'save', side_effect=OSError('injected index failure')):
            with self.assertRaises(OSError):
                self.invoke()
        self.assertEqual((self.root / '.vimax/sessions.json').read_bytes(), self.old_index)
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 2)
        self.invoke()
        self.assertEqual(len(json.loads((self.root / '.vimax/sessions.json').read_text(encoding='utf-8'))['sessions']), 2)

    def test_conflicting_retry_does_not_change_database_or_artifacts(self):
        self.invoke()
        before = publisher.database_rows(self.root / '.vimax')
        artifact = self.root / '.working_dir' / run('V02')['vimax_project_id'] / 'dialogue/messages.json'
        old_artifact = artifact.read_bytes()
        value = run('V02')
        value['messages'][0]['content'] = 'different'
        self.run_path.write_text(json.dumps(value))
        with self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(publisher.database_rows(self.root / '.vimax'), before)
        self.assertEqual(artifact.read_bytes(), old_artifact)

    def test_export_failure_leaves_database_and_index_untouched(self):
        self.core.export_xlsx = lambda value: (_ for _ in ()).throw(OSError('export failure'))
        with self.assertRaises(OSError):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)
        self.assertEqual((self.root / '.vimax/sessions.json').read_bytes(), self.old_index)

    def test_unselected_completed_draft_preserves_review_errors_and_label(self):
        value = run('V02')
        value['errors'] = ['manual review rejected this draft']
        self.run_path.write_text(json.dumps(value))
        self.argv.remove('--selected')
        self.invoke()
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['errors'], value['errors'])
        index = json.loads((self.root / '.vimax/sessions.json').read_text(encoding='utf-8'))
        self.assertIn('待优化', index['sessions'][value['vimax_project_id']]['project_name'])

    def test_selected_v03_is_supported(self):
        self.run_path.write_text(json.dumps(run('V03')))
        self.argv[self.argv.index('--version') + 1] = 'V03'
        self.invoke()
        self.assertIn(run('V03')['id'], publisher.database_rows(self.root / '.vimax'))

    def test_flow4_snapshots_install_before_database_write_and_retry_identically(self):
        value, source, target, payloads = self.snapshot_fixture()
        calls = []
        self.engine.contract_errors = lambda template, current: calls.append(current['config']['dialogue_flow_version']) or []
        self.invoke()
        self.invoke()
        self.assertEqual(calls, [4, 4])
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, payloads)
        self.assertEqual({p.name: p.read_bytes() for p in source.iterdir()}, payloads)
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['candidate_versions'], value['candidate_versions'])
        self.assertEqual(saved['candidate_versions'][0]['capture_status'], 'running')

    def test_snapshot_checksum_failure_does_not_write_database_or_index(self):
        value, source, target, payloads = self.snapshot_fixture()
        (source / 'messages.xlsx').write_bytes(b'corrupted')
        with self.assertRaisesRegex(AssertionError, 'checksum mismatch'):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)
        self.assertEqual((self.root / '.vimax/sessions.json').read_bytes(), self.old_index)
        self.assertFalse(target.exists())

    def test_conflicting_installed_snapshot_is_not_overwritten(self):
        value, source, target, payloads = self.snapshot_fixture()
        target.mkdir(parents=True)
        for name, payload in payloads.items():
            (target / name).write_bytes(payload)
        (target / 'messages.md').write_bytes(b'older immutable content')
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        with self.assertRaisesRegex(AssertionError, 'Immutable snapshot conflict'):
            self.invoke()
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, before)
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)

    def test_snapshot_install_failure_keeps_database_unchanged_and_can_retry(self):
        value, source, target, payloads = self.snapshot_fixture()
        write = importer.atomic_write
        def fail_snapshot(path, payload):
            if '.import-' in path.parent.name and path.name == 'messages.md':
                raise OSError('snapshot write failure')
            return write(path, payload)
        with patch.object(importer, 'atomic_write', fail_snapshot):
            with self.assertRaisesRegex(OSError, 'snapshot write failure'):
                self.invoke()
        self.assertFalse(target.exists())
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)
        self.assertEqual((self.root / '.vimax/sessions.json').read_bytes(), self.old_index)
        self.invoke()
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, payloads)

    def test_snapshot_path_escape_is_rejected_before_writes(self):
        value, source, target, payloads = self.snapshot_fixture()
        value['candidate_versions'][0]['path'] = '../outside'
        self.run_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(AssertionError, 'Unsafe snapshot path'):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)
        self.assertFalse(target.exists())

    def test_selected_flow4_contract_rejection_blocks_import(self):
        value, source, target, payloads = self.snapshot_fixture()
        self.engine.contract_errors = lambda template, current: ['截图消息未使用图片类型']
        with self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)
        self.assertFalse(target.exists())



class EvolutionImportSafety(ImportSafety):
    def evolution_fixture(self, status='paused', errors=None, label='V06'):
        family, revision = importer.version_parts(label)
        value = run(f'V{family:02d}')
        if revision:
            value['id'] = 'c' * 30 + f'{family:02d}'
        source = self.root / label
        source.mkdir()
        self.run_path = source / 'run.json'
        self.argv[self.argv.index('--run') + 1] = str(self.run_path)
        value.pop('vimax_project_id')
        value.update(status=status, errors=errors or [], program_review_deferred=status == 'paused',
                     review_progress={'phase': 'awaiting_human_review' if status == 'paused' else 'failed'},
                     version_record={'version': f'V{family:02d}', 'parent_version': 'V05', 'review_mode': 'deferred'})
        if revision:
            value['version_record']['revision'] = revision
        self.run_path.write_text(json.dumps(value), encoding='utf-8')
        digest = hashlib.sha256(importer.canonical([{key:m.get(key) for key in importer.MESSAGE_FIELDS}
                                                  for m in value['messages']]).encode()).hexdigest()
        reports = []
        hashes = {'run.json': hashlib.sha256(self.run_path.read_bytes()).hexdigest()}
        for name in ('R000-primary-review.md', 'R000-subagent-review.md'):
            path = source / 'reviews' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('Full read of 150 lines, actual draft: ' + digest, encoding='utf-8')
            reports.append(label + '/reviews/' + name)
            hashes['reviews/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
        review = {'version': label, 'message_sha256': digest, 'verdict': 'candidate' if status == 'paused' else 'needs_revision',
                  'summary': 'real human fixture summary', 'program_review_deferred': status == 'paused', 'reports': reports}
        review_path = source / 'review-index.json'
        review_path.write_text(json.dumps(review), encoding='utf-8')
        hashes['review-index.json'] = hashlib.sha256(review_path.read_bytes()).hexdigest()
        (source / 'hashes.json').write_text(json.dumps(hashes), encoding='utf-8')
        self.argv.remove('--selected')
        self.argv[self.argv.index('--version') + 1] = label
        return value

    def test_deferred_import_exact_retry_preserves_original_and_status(self):
        value = self.evolution_fixture()
        original = self.run_path.read_bytes()
        self.invoke(); self.invoke()
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['status'], 'paused')
        self.assertEqual(saved['messages'], value['messages'])
        self.assertTrue(saved['publication']['read_only'])
        self.assertEqual(saved['publication']['program_review_state'], 'deferred')
        self.assertNotIn('outcome_review', saved)
        self.assertEqual(saved['vimax_project_id'], 'dialogue-' + value['id'])
        self.assertEqual(self.run_path.read_bytes(), original)
        index = json.loads((self.root / '.vimax/sessions.json').read_text(encoding='utf-8'))
        self.assertIn('人工候选', index['sessions'][saved['vimax_project_id']]['project_name'])
        self.assertIn('模型待审', index['sessions'][saved['vimax_project_id']]['project_name'])
        self.assertEqual(index['active_session_id'], 'old-project')

    def test_deferred_actual_contract_errors_block_import(self):
        self.evolution_fixture()
        calls = []
        self.engine.contract_errors = lambda source, current: calls.append(current['status']) or ['actual full contract rejected']
        with self.assertRaisesRegex(AssertionError, 'actual full-draft contracts'):
            self.invoke()
        self.assertEqual(calls, ['completed'])
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)

    def test_missing_or_wrong_human_report_blocks_import(self):
        self.evolution_fixture()
        (self.run_path.parent / 'reviews/R000-subagent-review.md').write_text('another draft', encoding='utf-8')
        with self.assertRaisesRegex(AssertionError, 'not bound'):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)

    def test_failed_complete_original_remains_failed_with_same_errors(self):
        value = self.evolution_fixture(status='failed', errors=['original structural failure'])
        self.engine.contract_errors = lambda source, current: ['actual structural failure']
        self.invoke()
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['errors'], ['original structural failure'])
        self.assertEqual(saved['publication']['program_review_state'], 'not_completed')
        self.assertEqual(saved['publication']['contract_errors'], ['actual structural failure'])

    def test_new_source_metadata_same_id_cannot_overwrite_history(self):
        self.evolution_fixture(); self.invoke()
        original_rows = publisher.database_rows(self.root / '.vimax')
        value = json.loads(self.run_path.read_bytes()); value['unknown_provenance'] = 'changed'
        self.run_path.write_text(json.dumps(value), encoding='utf-8')
        hashes_path = self.run_path.parent / 'hashes.json'
        hashes = json.loads(hashes_path.read_bytes()); hashes['run.json'] = hashlib.sha256(self.run_path.read_bytes()).hexdigest()
        hashes_path.write_text(json.dumps(hashes), encoding='utf-8')
        with self.assertRaisesRegex(AssertionError, 'different content'):
            self.invoke()
        self.assertEqual(publisher.database_rows(self.root / '.vimax'), original_rows)


    def test_revision_label_imports_exact_original_without_replacing_family(self):
        value = self.evolution_fixture(label='V32-R001')
        original = self.run_path.read_bytes()
        self.invoke(); self.invoke()
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['version_record']['version'], 'V32')
        self.assertEqual(saved['version_record']['revision'], 'R001')
        self.assertEqual(saved['publication']['version'], 'V32-R001')
        self.assertEqual(self.run_path.read_bytes(), original)
        index = json.loads((self.root / '.vimax/sessions.json').read_bytes())
        self.assertTrue(index['sessions'][saved['vimax_project_id']]['project_name'].startswith('V32-R001'))

    def test_revision_label_mismatch_is_rejected(self):
        self.evolution_fixture(label='V32-R001')
        self.argv[self.argv.index('--version') + 1] = 'V32-R002'
        with self.assertRaisesRegex(AssertionError, 'Version label differs'):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.root / '.vimax')), 1)

    def test_exact_hash_correction_is_annotation_only(self):
        value = self.evolution_fixture(label='V32')
        source_hash = hashlib.sha256(self.run_path.read_bytes()).hexdigest()
        corrections = {'schema_version': 1, 'notes': [{'version': 'V32', 'run_sha256': source_hash,
                       'display_note': '原稿保留为时间配置备选', 'representative_version': 'V32-R001'}]}
        (self.root / 'evolution-corrections.json').write_text(json.dumps(corrections), encoding='utf-8')
        self.invoke(); self.invoke()
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['status'], value['status'])
        self.assertEqual(saved['publication']['human_verdict'], 'candidate')
        self.assertEqual(saved['publication']['display_note'], '原稿保留为时间配置备选')
        self.assertEqual(saved['publication']['representative_version'], 'V32-R001')
        self.assertEqual(hashlib.sha256(self.run_path.read_bytes()).hexdigest(), source_hash)

    def test_wrong_hash_correction_never_annotates_another_original(self):
        value = self.evolution_fixture(label='V32')
        (self.root / 'evolution-corrections.json').write_text(json.dumps({'schema_version': 1, 'notes': [
            {'version': 'V32', 'run_sha256': 'wrong', 'display_note': 'do not attach', 'representative_version': 'V32-R001'}]}))
        self.invoke()
        saved = json.loads(publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertNotIn('display_note', saved['publication'])
        self.assertNotIn('representative_version', saved['publication'])


class PublicationArtifacts(unittest.TestCase):
    def test_verify_state_supports_revision_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture(root)
            state = root / '.vimax'
            originals = publisher.database_rows(state)
            old_index = json.loads((state / 'sessions.json').read_bytes())
            archive = root / 'archive'
            version_folder = archive / 'V32-R001'
            version_folder.mkdir(parents=True)
            value = run('V32')
            value['version_record'] = {'version': 'V32', 'revision': 'R001'}
            source = version_folder / 'run.json'
            source.write_text(json.dumps(value), encoding='utf-8')
            (version_folder / 'review-index.json').write_text('{"verdict":"candidate"}', encoding='utf-8')
            target = {**value, 'publication': {'read_only': True, 'source_run_sha256': publisher.digest(source),
                                              'human_verdict': 'candidate'}}
            with closing(sqlite3.connect(state / 'dialogue/app.sqlite3')) as db:
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (value['id'], 'template', json.dumps(target), value['created_at']))
                db.commit()
            with patch.object(publisher, 'ARCHIVE', archive), patch.object(publisher, 'VERSIONS', ('V32-R001',)):
                publisher.verify_state(state, originals, old_index)

    def test_plain_and_revision_family_numbers(self):
        self.assertEqual(importer.version_parts('V32-R001'), (32, 'R001'))
        self.assertEqual(importer.version_parts('V32'), (32, None))
        self.assertEqual(publisher.family_number('V32-R001'), 32)
        for label in ('V32-R1', 'V32-R001/other', '32', 'V32-bad'):
            with self.assertRaises(AssertionError):
                publisher.family_number(label)

    def test_runtime_locks_and_temporary_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = ['run.json', 'snapshots/R000-1234/manifest.json', 'reviews/R000-primary-review.md',
                     'attempts/call-001.json', 'sources/evolve-natural.py', '.runtime/app.sqlite3',
                     '.runtime/app.sqlite3-wal', 'oops.sqlite3', 'work.lock', '.upload-tmp', 'progress.json.tmp',
                     'run.json.tmp-abc', '__pycache__/example.pyc']
            for name in paths:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
            selected = {path.relative_to(root).as_posix() for path in root.rglob('*') if publisher.publishable_artifact(path, root)}
            self.assertEqual(selected, set(paths[:5]))

class PublishSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.state = self.root / '.vimax'
        self.archive = self.state / 'dialogue-versions' / publisher.BATCH
        self.archive.mkdir(parents=True)
        for version in ('V01', 'V02'):
            (self.archive / version).mkdir()
            (self.archive / version / 'run.json').write_text(json.dumps(run(version)))
        self.manifest = {p.relative_to(self.archive).as_posix(): publisher.digest(p)
                         for p in self.archive.rglob('*') if p.is_file()}
        (self.archive / 'sha256.json').write_text(json.dumps(self.manifest))
        (self.archive.parent / (publisher.BATCH + '.candidate-passed.json')).write_text(json.dumps({
            'sha256': self.manifest, 'selected': publisher.SELECTED, 'versions': list(publisher.VERSIONS),
            'image_id': 'backend-id', 'web_image_id': 'web-id',
            'previous_release': str(self.root / 'opt/vimax/releases/20261014-a335ea6')}))
        self.old = self.root / 'opt/vimax/releases/20261014-a335ea6'
        self.new = self.root / ('opt/vimax/releases/' + publisher.BATCH)
        self.old.mkdir(parents=True)
        self.new.mkdir(parents=True)
        (self.new / 'release.env').write_text('VIMAX_RELEASE=' + publisher.BATCH)
        (self.root / 'opt/backups').mkdir(parents=True)
        self.current_target = self.old
        self.containers = {publisher.BACKEND: True, publisher.WORKBENCH: True}
        self.events = []
        self.fail_import = False
        self.fail_health = False
        self.fail_reopen = False
        self.health_calls = 0
        self.backend_image = 'vimax-dialogue-backend:20261014-a335ea6'
        self.web_image = 'vimax-dialogue-web:20261014-a335ea6'
        self.patches = []
        def mapped_path(value):
            value = str(value)
            if value == '/opt/vimax/current':
                return types.SimpleNamespace(resolve=lambda: self.current_target)
            return self.root / value.lstrip('/') if value.startswith('/opt/') else Path(value)
        for name, value in [('STATE', self.state), ('ARCHIVE', self.archive), ('OLD', str(self.old)),
                            ('RELEASE', str(self.new)), ('Path', mapped_path),
                            ('command', self.command), ('compose', self.compose),
                            ('health', self.health), ('import_runs', self.import_runs),
                            ('verify_runtime', lambda: None), ('link_to', self.link_to)]:
            p = patch.object(publisher, name, value)
            p.start()
            self.patches.append(p)
        p = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=lambda *args: None, LOCK_EX=1, LOCK_NB=2)})
        p.start()
        self.patches.append(p)
        self.originals = publisher.database_rows(self.state)
        self.original_index = (self.state / 'sessions.json').read_bytes()

    def tearDown(self):
        for p in reversed(getattr(self, 'patches', [])):
            p.stop()
        self.temp.cleanup()

    def command(self, *parts):
        self.events.append(parts)
        if parts[:2] == ('docker', 'stop'):
            self.containers[parts[-1]] = False
        elif parts[:2] == ('docker', 'start'):
            if self.fail_reopen:
                raise RuntimeError('ambiguous docker start failure')
            self.containers[parts[-1]] = True
        elif parts[:2] == ('docker', 'inspect'):
            if parts[-1] == '{{.Config.Image}}':
                return self.backend_image if parts[2] == publisher.BACKEND else self.web_image
            return 'true' if self.containers[parts[2]] else 'false'
        elif parts[:3] == ('docker', 'image', 'inspect'):
            return 'web-id' if parts[3].startswith('vimax-dialogue-web:') else 'backend-id'
        return ''

    def compose(self, release, *args):
        self.events.append(('compose', release, args[-1]))
        if args[-1] == 'workbench':
            if self.fail_reopen and release == str(self.new):
                raise RuntimeError('ambiguous workbench Compose failure')
            self.web_image = 'vimax-dialogue-web:' + Path(release).name
            self.containers[publisher.WORKBENCH] = True
        else:
            self.backend_image = 'vimax-dialogue-backend:' + Path(release).name
            self.containers[publisher.BACKEND] = True

    def health(self):
        self.health_calls += 1
        if self.fail_health and self.health_calls == 1:
            raise RuntimeError('injected health failure')

    def import_runs(self, state, working):
        with closing(sqlite3.connect(state / 'dialogue/app.sqlite3')) as db:
            for version in publisher.VERSIONS:
                value = json.loads((self.archive / version / 'run.json').read_text(encoding='utf-8'))
                db.execute('insert or ignore into sessions values(?,?,?,?)',
                           (value['id'], value['template_id'], json.dumps(value), value['created_at']))
                if self.fail_import:
                    db.commit()
                    raise RuntimeError('injected half import failure')
            db.commit()
        data = json.loads((state / 'sessions.json').read_text(encoding='utf-8'))
        for version in publisher.VERSIONS:
            data['sessions'][run(version)['vimax_project_id']] = {'version': version}
        (state / 'sessions.json').write_text(json.dumps(data))
        working.mkdir(parents=True, exist_ok=True)

    def link_to(self, target):
        self.current_target = Path(target)

    def invoke(self, mode='publish'):
        with contextlib.redirect_stdout(io.StringIO()):
            publisher.remote(mode)

    def assert_restored(self):
        self.assertEqual(publisher.database_rows(self.state), self.originals)
        self.assertEqual((self.state / 'sessions.json').read_bytes(), self.original_index)
        self.assertTrue(self.containers[publisher.WORKBENCH])
        self.assertTrue(self.containers[publisher.BACKEND])
        self.assertEqual(self.current_target, self.old)
        self.assertEqual(self.web_image, 'vimax-dialogue-web:20261014-a335ea6')

    def test_running_generation_is_not_interrupted(self):
        with closing(sqlite3.connect(self.state / 'dialogue/app.sqlite3')) as db:
            db.execute('update sessions set run_json=?', (json.dumps({'id': 'old', 'status': 'running'}),))
            db.commit()
        with self.assertRaisesRegex(RuntimeError, 'publication deferred'):
            self.invoke()
        self.assertTrue(self.containers[publisher.WORKBENCH])
        self.assertNotIn(('docker', 'stop', '--time', '30', publisher.BACKEND), self.events)

    def test_half_import_failure_restores_same_window_snapshot(self):
        self.fail_import = True
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assert_restored()
        self.assertGreater(self.health_calls, 0)

    def test_restore_uses_python36_path_unlink_signature(self):
        original_unlink = Path.unlink
        def legacy_unlink(path):
            return original_unlink(path)
        self.fail_import = True
        with patch.object(Path, 'unlink', legacy_unlink):
            with self.assertRaises(RuntimeError):
                self.invoke()
        self.assert_restored()

    def test_new_backend_health_failure_verifies_old_backend_recovery(self):
        self.fail_health = True
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assert_restored()
        self.assertEqual(self.health_calls, 2)

    def test_success_keeps_old_data_and_restores_entry(self):
        self.invoke()
        self.assertEqual(len(publisher.database_rows(self.state)), 3)
        self.assertEqual(self.current_target, self.new)
        self.assertTrue(self.containers[publisher.WORKBENCH])
        self.assertTrue((self.archive.parent / 'batches.json').exists())
        self.assertEqual(self.web_image, publisher.WEB_IMAGE)
        self.assertIn(('compose', str(self.new), 'workbench'), self.events)
        self.assertFalse(any(event[:2] == ('docker', 'tag') for event in self.events))

    def test_start_ambiguity_never_rolls_back_potential_new_writes(self):
        self.fail_reopen = True
        with self.assertRaisesRegex(RuntimeError, 'NO automatic DB rollback'):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.state)), 3)
        self.assertEqual(self.current_target, self.new)

    def test_archive_tampering_fails_before_any_container_change(self):
        (self.archive / 'V02/run.json').write_text('{}')
        with self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(self.events, [])

    def test_candidate_receipt_pins_backend_and_workbench_images(self):
        self.invoke('candidate')
        receipt = json.loads((self.archive.parent / (publisher.BATCH + '.candidate-passed.json')).read_text(encoding='utf-8'))
        self.assertEqual(receipt['image_id'], 'backend-id')
        self.assertEqual(receipt['web_image_id'], 'web-id')
        self.assertFalse(any(event[:2] == ('docker', 'stop') for event in self.events))

    def test_missing_workbench_image_fails_before_stopping_entry(self):
        original = self.command
        def missing_image(*parts):
            if parts[:4] == ('docker', 'image', 'inspect', publisher.WEB_IMAGE):
                raise RuntimeError('missing workbench image')
            return original(*parts)
        with patch.object(publisher, 'command', missing_image):
            with self.assertRaisesRegex(RuntimeError, 'missing workbench image'):
                self.invoke()
        self.assertTrue(self.containers[publisher.WORKBENCH])
        self.assertFalse(any(event[:2] == ('docker', 'stop') for event in self.events))

    def test_changed_workbench_image_requires_new_candidate_verification(self):
        receipt_path = self.archive.parent / (publisher.BATCH + '.candidate-passed.json')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        receipt['web_image_id'] = 'different-web-id'
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(AssertionError, 'Workbench image changed'):
            self.invoke()
        self.assertTrue(self.containers[publisher.WORKBENCH])
        self.assertFalse(any(event[:2] == ('docker', 'stop') for event in self.events))

    def test_v03_selected_imports_all_three_versions(self):
        value = run('V03')
        (self.archive / 'V03').mkdir()
        (self.archive / 'V03/run.json').write_text(json.dumps(value))
        manifest = {p.relative_to(self.archive).as_posix(): publisher.digest(p)
                    for p in self.archive.rglob('*') if p.is_file() and p.name != 'sha256.json'}
        (self.archive / 'sha256.json').write_text(json.dumps(manifest))
        (self.archive.parent / (publisher.BATCH + '.candidate-passed.json')).write_text(json.dumps({
            'sha256': manifest, 'selected': 'V03', 'versions': ['V01', 'V02', 'V03'],
            'image_id': 'backend-id', 'web_image_id': 'web-id', 'previous_release': str(self.old)}))
        original_import = self.import_runs
        def import_three(state, working):
            original_import(state, working)
        with patch.object(publisher, 'VERSIONS', ('V01', 'V02', 'V03')), patch.object(publisher, 'SELECTED', 'V03'), patch.object(publisher, 'import_runs', import_three):
            self.invoke()
        self.assertEqual(len(publisher.database_rows(self.state)), 4)
        self.assertEqual(json.loads((self.archive.parent / 'batches.json').read_text(encoding='utf-8'))[publisher.BATCH]['selected'], 'V03')

    def use_previous_551_and_only_v03(self):
        self.old = self.root / 'opt/vimax/releases/20261015-551d9da'
        self.old.mkdir(parents=True)
        self.current_target = self.old
        self.backend_image = 'vimax-dialogue-backend:20261015-551d9da'
        self.web_image = 'vimax-dialogue-web:20261015-551d9da'
        value = run('V03')
        value['config']['dialogue_flow_version'] = 4
        (self.archive / 'V03').mkdir()
        (self.archive / 'V03/run.json').write_text(json.dumps(value))
        manifest = {p.relative_to(self.archive).as_posix(): publisher.digest(p)
                    for p in self.archive.rglob('*') if p.is_file() and p.name != 'sha256.json'}
        (self.archive / 'sha256.json').write_text(json.dumps(manifest))
        (self.archive.parent / (publisher.BATCH + '.candidate-passed.json')).write_text(json.dumps({
            'sha256': manifest, 'selected': 'V03', 'versions': ['V03'], 'image_id': 'backend-id',
            'web_image_id': 'web-id', 'previous_release': str(self.old)}))
        return value

    def test_release_from_551_imports_only_v03_keeps_existing_rows(self):
        value = self.use_previous_551_and_only_v03()
        with patch.object(publisher, 'OLD', str(self.old)), patch.object(publisher, 'VERSIONS', ('V03',)), patch.object(publisher, 'SELECTED', 'V03'):
            self.invoke()
        rows = publisher.database_rows(self.state)
        self.assertEqual(set(rows), {'old', value['id']})
        self.assertEqual(rows['old'], self.originals['old'])
        record = json.loads((self.archive.parent / 'batches.json').read_text(encoding='utf-8'))[publisher.BATCH]
        self.assertEqual(record['previous_release'], str(self.old))
        self.assertEqual(record['versions'], ['V03'])

    def test_release_failure_restores_551_images_and_old_rows(self):
        self.use_previous_551_and_only_v03()
        self.fail_health = True
        with patch.object(publisher, 'OLD', str(self.old)), patch.object(publisher, 'VERSIONS', ('V03',)), patch.object(publisher, 'SELECTED', 'V03'):
            with self.assertRaises(RuntimeError):
                self.invoke()
        self.assertEqual(publisher.database_rows(self.state), self.originals)
        self.assertEqual((self.state / 'sessions.json').read_bytes(), self.original_index)
        self.assertEqual(self.backend_image, 'vimax-dialogue-backend:20261015-551d9da')
        self.assertEqual(self.web_image, 'vimax-dialogue-web:20261015-551d9da')
        self.assertEqual(self.current_target, self.old)
        self.assertTrue(self.containers[publisher.WORKBENCH])


class CompatibilitySafety(unittest.TestCase):
    def test_explicit_previous_release_configuration_and_validation(self):
        fields = ('BATCH', 'OLD', 'RELEASE', 'IMAGE', 'WEB_IMAGE', 'ARCHIVE', 'VERSIONS', 'SELECTED')
        saved = {name: getattr(publisher, name) for name in fields}
        try:
            publisher.configure('2965645', 'V03', ['V03'], '20261015-551d9da')
            self.assertEqual(publisher.OLD, '/opt/vimax/releases/20261015-551d9da')
            self.assertEqual(publisher.VERSIONS, ('V03',))
            publisher.configure('2965645', 'V32-R001', ['V32', 'V32-R001'], '20261015-551d9da')
            self.assertEqual(publisher.VERSIONS, ('V32', 'V32-R001'))
            self.assertEqual(publisher.SELECTED, 'V32-R001')
            self.assertEqual(publisher.IMAGE, 'vimax-dialogue-backend:20261015-2965645')
            with self.assertRaises(AssertionError):
                publisher.configure('2965645', 'V03', ['V03'], '../551d9da')
            publisher.configure('2965645', 'V03', ['V03'])
            self.assertEqual(publisher.OLD, '/opt/vimax/releases/20261014-a335ea6')
        finally:
            for name, value in saved.items():
                setattr(publisher, name, value)

    def test_publisher_accepts_python36_syntax(self):
        ast.parse((HERE / 'publish-natural-safe.py').read_text(encoding='utf-8'), feature_version=(3, 6))

    def test_command_uses_python36_supported_subprocess_arguments(self):
        def legacy_run(args, stdout=None, stderr=None, universal_newlines=False, timeout=None):
            self.assertEqual(stdout, publisher.subprocess.PIPE)
            self.assertEqual(stderr, publisher.subprocess.PIPE)
            self.assertTrue(universal_newlines)
            self.assertEqual(timeout, 180)
            return types.SimpleNamespace(returncode=0, stdout='compatible\n', stderr='')
        with patch.object(publisher.subprocess, 'run', legacy_run):
            self.assertEqual(publisher.command('fake-command'), 'compatible')

    def test_python36_backup_fallback_uses_readonly_source_mount(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fixture(root)
            source_state = root / '.vimax'
            destination = root / 'snapshot'
            original_connect = sqlite3.connect
            events = []
            class NoBackupConnection:
                def __init__(self, connection, is_source):
                    self.connection = connection
                    self.is_source = is_source
                    self.closed = False
                def execute(self, sql):
                    cursor = self.connection.execute(sql)
                    if self.is_source and sql == 'pragma integrity_check':
                        def read_all():
                            result = cursor.fetchall()
                            events.append('source-integrity-read')
                            return result
                        return types.SimpleNamespace(fetchall=read_all)
                    return cursor
                def close(self):
                    self.closed = True
                    self.connection.close()
                    if self.is_source:
                        events.append('source-closed')
                def __getattr__(self, key):
                    if key == 'backup':
                        raise AttributeError(key)
                    return getattr(self.connection, key)
            def legacy_connect(database, **kwargs):
                self.assertIsInstance(database, str)
                return NoBackupConnection(original_connect(database, **kwargs),
                                          database == 'file:' + str(source_state / 'dialogue/app.sqlite3') + '?mode=ro')
            calls = []
            def fake_docker(*args):
                calls.append(args)
                self.assertEqual(events, ['source-integrity-read'])
                events.append('backup-container')
                self.assertIn((source_state / 'dialogue').as_posix() + ':/source:ro', args)
                self.assertIn((destination / 'dialogue').as_posix() + ':/backup', args)
                self.assertIn(publisher.IMAGE, args)
                with closing(original_connect('file:' + str(source_state / 'dialogue/app.sqlite3') + '?mode=ro', uri=True)) as source:
                    with closing(original_connect(str(destination / 'dialogue/app.sqlite3'))) as target:
                        source.backup(target)
                return ''
            with patch.object(publisher.sqlite3, 'connect', legacy_connect), patch.object(publisher, 'command', fake_docker):
                publisher.snapshot(source_state, destination)
            self.assertEqual(len(calls), 1)
            self.assertEqual(events, ['source-integrity-read', 'backup-container', 'source-closed'])
            self.assertEqual(publisher.database_rows(source_state), publisher.database_rows(destination))

    def test_source_integrity_failure_closes_connection_without_running_container(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fixture(root)
            events = []
            def read_all():
                events.append('source-integrity-read')
                return [('corruption detected',), ('second corruption detail',)]
            source = types.SimpleNamespace(
                execute=lambda sql: types.SimpleNamespace(fetchall=read_all),
                close=lambda: events.append('source-closed'))
            with patch.object(publisher.sqlite3, 'connect', return_value=source), patch.object(publisher, 'command') as container:
                with self.assertRaisesRegex(AssertionError, 'Source database integrity check failed'):
                    publisher.snapshot(root / '.vimax', root / 'snapshot')
                container.assert_not_called()
            self.assertEqual(events, ['source-integrity-read', 'source-closed'])
            self.assertFalse((root / 'snapshot/dialogue/app.sqlite3').exists())

    def test_windows_client_all_sftp_and_remote_command_paths_are_posix(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            helper = root / '.vimax/deploy/publish-natural-safe.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('test helper')
            (helper.parent / 'import-natural-dialogue.py').write_text('test importer')
            output = root / 'outputs/自然群聊-20261015'
            for version in ('V01', 'V02'):
                folder = output / version
                folder.mkdir(parents=True)
                (folder / 'run.json').write_text(json.dumps(run(version)))
                (folder / 'nested').mkdir()
                (folder / 'nested/review.md').write_text('review')
            (output / 'versions.json').write_text('{}')
            (output / 'evolution-corrections.json').write_text('{}')
            remote_paths = []
            remote_commands = []
            def checked(path):
                self.assertTrue(path.startswith('/opt/vimax/shared/state/dialogue-versions/'))
                self.assertNotIn('\\', path)
                remote_paths.append(path)
            class FakeSFTP:
                def file(inner, path, mode='r'):
                    checked(path)
                    if mode == 'r':
                        raise FileNotFoundError(path)
                    return io.StringIO()
                def mkdir(inner, path, mode): checked(path)
                def put(inner, local, remote):
                    self.assertTrue(Path(local).is_file())
                    checked(remote)
                def chmod(inner, path, mode): checked(path)
                def rename(inner, source, target):
                    checked(source)
                    checked(target)
                def close(inner): pass
            class FakeOutput:
                channel = types.SimpleNamespace(recv_exit_status=lambda: 0, set_combine_stderr=lambda value: None)
                def read(inner): return b''
                def __iter__(inner): return iter(())
            class FakeSSH:
                def load_system_host_keys(inner): pass
                def set_missing_host_key_policy(inner, value): pass
                def connect(inner, *args, **kwargs):
                    self.assertEqual(kwargs['password'], 'local-test-only')
                def open_sftp(inner): return FakeSFTP()
                def exec_command(inner, command, **kwargs):
                    self.assertNotIn('\\', command)
                    remote_commands.append(command)
                    return None, FakeOutput(), FakeOutput()
                def close(inner): pass
            fake_paramiko = types.SimpleNamespace(SSHClient=FakeSSH, AutoAddPolicy=lambda: None)
            windows_archive = PureWindowsPath('/opt/vimax/shared/state/dialogue-versions/20261015-74b2429')
            with patch.object(publisher, '__file__', str(helper)), patch.object(publisher, 'ARCHIVE', windows_archive), patch.object(publisher, 'BATCH', '20261015-74b2429'), patch.object(publisher, 'VERSIONS', ('V01', 'V02')), patch.object(publisher, 'SELECTED', 'V02'), patch.dict(sys.modules, {'paramiko': fake_paramiko}), patch.dict(publisher.os.environ, {'VIMAX_TASK_SSH_PASSWORD': 'local-test-only'}):
                publisher.upload_and_run('candidate')
            self.assertTrue(remote_paths)
            self.assertTrue(any(path.endswith('/evolution-corrections.json') for path in remote_paths))
            self.assertEqual(len(remote_commands), 2)
            self.assertIn('/opt/vimax/shared/state/dialogue-versions/20261015-74b2429/publish.py', remote_commands[-1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
