"""Offline fixtures/fake SSH only. No model, Docker, server or production DB calls."""
import contextlib
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


pub = load('independent_publisher_test', HERE / 'publish-independent-safe.py')
legacy = load('independent_legacy_fixtures', HERE / 'test-natural-publish-safety.py')
imp = legacy.importer


def corpus_bytes():
    texts = ['fixture-only reference', 'no network or model']
    digest = pub.sha(pub.canonical(texts))
    return pub.canonical({'schema_version': 1,
        'sources': [{'source_id': 'fixture-source', 'url': 'https://fixture.invalid/public', 'license': 'MIT fixture'}],
        'conversations': [{'id': digest, 'content_sha256': digest, 'turns': [{'text': text} for text in texts],
                           'attributions': [{'source_id': 'fixture-source'}]}]})


def returned_review(value, review):
    wording = [{key: message.get(key) for key in imp.MESSAGE_FIELDS} for message in value['messages']]
    return {'state': 'returned', 'messages_sha256': pub.sha(imp.canonical(wording).encode()),
            'message_count': len(value['messages']), 'review': review}


def evidence(value):
    value.setdefault('version_record', {})
    value['version_record'].update(method='independent_generation', series=pub.SERIES, parent_run=None)
    value['version_record'].pop('parent_version', None)
    cards = {'客户1': {'identity': 'fixture cast'}}
    value['config'].update(story_world='fresh-world-' + value['id'], persona_cards=cards)
    value['creative_variation'] = {'method': 'independent_generation', 'parent_run': None,
                                   'style_label': '长短错落 · 选择性回应', 'persona_cards': cards}
    value['independence'] = {'method': 'independent_generation', 'parent_run': None,
                             'initial_messages': 0, 'initial_story': None,
                             'fresh_story_world': value['config']['story_world']}
    value['version_record']['corpus'] = {'file_sha256': pub.sha(corpus_bytes())}
    value['corpus_reference'] = {'sources': json.loads(corpus_bytes())['sources'],
                                  'samples': [{'turns': ['fixture only', 'not live data']}]}
    return value


class IndependentImportSafety(legacy.EvolutionImportSafety):
    # Inherited tests also verify legacy V01..03 and evolution/revision behavior.
    def independent_fixture(self, status='paused', label='V01'):
        value = evidence(self.evolution_fixture(status=status, errors=['original rejection'] if status == 'failed' else [], label=label))
        wording = [{key: m.get(key) for key in imp.MESSAGE_FIELDS} for m in value['messages']]
        fingerprint = pub.sha(imp.canonical(wording).encode())
        name = 'R000-' + fingerprint[:8]
        record = {'hash': fingerprint, 'path': 'versions/' + value['id'] + '/' + name}
        value['candidate_versions'] = [record]
        source = self.run_path.parent / 'snapshots' / name; source.mkdir(parents=True)
        payloads = {'run.json': json.dumps(value).encode(), 'messages.md': b'full fixture draft', 'messages.xlsx': b'fixture xlsx'}
        payloads['manifest.json'] = json.dumps({'record': record, 'hash_fields': list(imp.MESSAGE_FIELDS), 'message_count': 150,
            'files': {key: pub.sha(raw) for key, raw in payloads.items()}}).encode()
        for key, raw in payloads.items(): (source / key).write_bytes(raw)
        self.save_independent(value)
        return value

    def save_independent(self, value):
        self.run_path.write_text(json.dumps(value), encoding='utf-8')
        path = self.run_path.parent / 'hashes.json'; hashes = json.loads(path.read_bytes())
        hashes['run.json'] = pub.sha(self.run_path.read_bytes())
        path.write_text(json.dumps(hashes), encoding='utf-8')

    def test_independent_v01_requires_full_evidence_and_preserves_old52_exactly(self):
        value = self.independent_fixture()
        database = self.root / '.vimax/dialogue/app.sqlite3'
        with contextlib.closing(sqlite3.connect(database)) as db:
            for i in range(51): db.execute('insert into sessions values(?,?,?,?)', (f'legacy-{i}', 'template', json.dumps({'old': i}), 'today'))
            db.commit()
        index_path = self.root / '.vimax/sessions.json'; index = json.loads(index_path.read_bytes())
        for i in range(51): index['sessions'][f'legacy-{i}'] = {'project_name': f'old{i}', 'session_id': f'legacy-{i}'}
        index_path.write_text(json.dumps(index), encoding='utf-8')
        before = legacy.publisher.database_rows(self.root / '.vimax'); raw = self.run_path.read_bytes()
        self.invoke(); self.invoke()
        after = legacy.publisher.database_rows(self.root / '.vimax')
        self.assertEqual({key: after[key] for key in before}, before)
        actual_index = json.loads(index_path.read_bytes())
        self.assertEqual(actual_index['active_session_id'], index['active_session_id'])
        self.assertEqual({key: actual_index['sessions'][key] for key in index['sessions']}, index['sessions'])
        saved = json.loads(after[value['id']]); title = actual_index['sessions'][saved['vimax_project_id']]['project_name']
        self.assertTrue(title.startswith('独立01 · 长短错落'))
        self.assertIn('模型待审', title)
        self.assertIn('完整双审候选', title); self.assertNotIn('人工候选', title)
        self.assertEqual(saved['messages'], value['messages']); self.assertEqual(saved['status'], 'paused')
        for key in ('creative_variation', 'independence', 'corpus_reference', 'version_record'):
            self.assertEqual(saved[key], value[key])
        self.assertTrue(saved['publication']['read_only']); self.assertIsNone(saved['publication']['parent_run'])
        self.assertFalse(saved['publication']['selected']); self.assertFalse(saved['publication']['user_selected'])
        self.assertEqual(self.run_path.read_bytes(), raw)

    def test_independent_v01_cannot_take_legacy_no_review_or_no_snapshot_path(self):
        value = self.independent_fixture(); value['candidate_versions'] = []; self.save_independent(value)
        with self.assertRaisesRegex(AssertionError, 'requires archived snapshots'): self.invoke()
        self.assertEqual(len(legacy.publisher.database_rows(self.root / '.vimax')), 1)

    def test_markdown_discloses_fictional_rehearsal_only_when_configured(self):
        value = self.independent_fixture()
        self.invoke()
        path = self.root / '.working_dir' / ('dialogue-' + value['id']) / 'dialogue/messages.md'
        original = path.read_bytes()
        expected = '\n\n'.join(f"{message['seq']} **{message['sender']}**：{message['content']}" for message in value['messages']).encode('utf-8')
        self.assertEqual(original, expected)
        # A different isolated target receives the new configuration, without
        # changing the source/identity of the already imported old artifact.
        value['id'] = 'f' * 32
        value['config']['usage_context'] = 'fictional_rehearsal'
        value['candidate_versions'] = []
        self.save_independent(value)
        payloads = []
        write = imp.atomic_write
        def capture(path, payload):
            if path.name == 'messages.md': payloads.append(payload)
            return write(path, payload)
        # Snapshot enforcement was separately verified; preserve its call here
        # while using this case only to compare the exported byte contract.
        value['candidate_versions'] = [{'fixture': True}]
        self.save_independent(value)
        with patch.object(imp, 'prepare_snapshots', return_value=[]), patch.object(imp, 'atomic_write', side_effect=capture):
            self.invoke()
        prefix = '# 虚构示例／内部排练稿\n\n人物及行为为虚构情节，不代表真实参与记录。\n\n'.encode('utf-8')
        self.assertEqual(payloads, [prefix + expected])
        self.assertEqual(path.read_bytes(), original)

    def test_independent_revision_is_fresh_parentless_attempt_and_keeps_full_label(self):
        value = self.independent_fixture(label='V01-R001')
        self.invoke(); self.invoke()
        saved = json.loads(legacy.publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['version_record']['version'], 'V01')
        self.assertEqual(saved['version_record']['revision'], 'R001')
        self.assertIsNone(saved['version_record']['parent_run'])
        self.assertEqual(saved['publication']['display_label'], '独立01-R001')
        index = json.loads((self.root / '.vimax/sessions.json').read_bytes())
        self.assertTrue(index['sessions'][saved['vimax_project_id']]['project_name'].startswith('独立01-R001 · '))

    def test_independent_v01_wrong_human_hash_rejected_before_any_write(self):
        self.independent_fixture()
        (self.run_path.parent / 'reviews/R000-subagent-review.md').write_text('different draft')
        with self.assertRaisesRegex(AssertionError, 'not bound'): self.invoke()
        self.assertEqual(len(legacy.publisher.database_rows(self.root / '.vimax')), 1)

    def test_independent_selected_requires_actual_full_model_acceptance(self):
        value = self.independent_fixture(status='completed')
        value['program_review_history'] = [returned_review(value, {'goal_met': False, 'issues': ['real model rejection']})]
        review_path = self.run_path.parent / 'review-index.json'; review = json.loads(review_path.read_bytes())
        review['verdict'] = 'candidate'; review_path.write_text(json.dumps(review), encoding='utf-8')
        hashes_path = self.run_path.parent / 'hashes.json'; hashes = json.loads(hashes_path.read_bytes())
        hashes['review-index.json'] = pub.sha(review_path.read_bytes()); hashes_path.write_text(json.dumps(hashes), encoding='utf-8')
        self.save_independent(value); self.argv.append('--selected')
        with self.assertRaisesRegex(AssertionError, 'model review did not pass'): self.invoke()
        good = returned_review(value, {'goal_met': True, 'issues': []})
        for changes in ({'state': 'failed'}, {'messages_sha256': '0' * 64}, {'message_count': 149}):
            value['program_review_history'] = [{**good, **changes}]; self.save_independent(value)
            with self.assertRaisesRegex(AssertionError, 'not returned and bound'): self.invoke()
        value['program_review_history'] = [good]; self.save_independent(value)
        self.invoke(); self.invoke()
        saved = json.loads(legacy.publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertTrue(saved['publication']['selected']); self.assertEqual(saved['status'], 'completed')

    def test_actual_shaped_final_call_failure_does_not_reuse_earlier_success_or_outcome(self):
        value = self.independent_fixture(status='failed')
        earlier = returned_review(value, {'goal_met': True, 'issues': []})
        earlier['messages_sha256'] = '1' * 64
        final = returned_review(value, {})
        final.pop('review'); final.update(state='failed', error='RuntimeError: model connection or response format failed')
        value.update(program_review_history=[earlier, final], outcome_review={'met': True, 'reason': 'stale earlier result'})
        self.save_independent(value); self.invoke(); self.invoke()
        saved = json.loads(legacy.publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(saved['publication']['program_review_state'], 'not_completed')
        self.assertEqual(saved['program_review_history'], value['program_review_history'])
        self.assertEqual(saved['outcome_review'], value['outcome_review'])
        self.assertEqual(saved['status'], 'failed'); self.assertEqual(saved['errors'], value['errors'])
        index = json.loads((self.root / '.vimax/sessions.json').read_bytes())
        title = index['sessions'][saved['vimax_project_id']]['project_name']
        self.assertIn('模型审阅未完成', title); self.assertNotIn('模型已全文复核', title)

    def test_last_returned_call_must_cover_current_hash_and_count_even_when_goal_is_true(self):
        value = self.independent_fixture(status='failed')
        good = returned_review(value, {'goal_met': True, 'issues': []})
        for changes in ({'state': 'started'}, {'messages_sha256': '2' * 64}, {'message_count': 149}):
            value['program_review_history'] = [good, {**good, **changes}]
            self.assertEqual(imp.independent_review_state(value), 'not_completed')
        value['program_review_history'] = [good]
        self.assertEqual(imp.independent_review_state(value), 'reviewed')

    def test_independent_wrong_parent_or_inherited_initial_context_rejected(self):
        value = self.independent_fixture(); value['version_record']['parent_run'] = 'old-run'; self.save_independent(value)
        with self.assertRaisesRegex(AssertionError, 'inherit a parent'): self.invoke()
        value['version_record']['parent_run'] = None; value['independence']['initial_messages'] = 150; self.save_independent(value)
        with self.assertRaises(AssertionError): self.invoke()
        self.assertEqual(len(legacy.publisher.database_rows(self.root / '.vimax')), 1)

    def test_independent_style_cast_and_real_corpus_evidence_cannot_be_omitted(self):
        value = self.independent_fixture()
        original = json.loads(json.dumps(value))
        for change in ('style', 'cast', 'corpus'):
            value = json.loads(json.dumps(original))
            if change == 'style': value['creative_variation']['style_label'] = ''
            if change == 'cast': value['creative_variation']['persona_cards'] = {'old': {'identity': 'copied cast'}}
            if change == 'corpus': value['corpus_reference']['samples'] = []
            self.save_independent(value)
            with self.assertRaises(AssertionError): self.invoke()
        self.assertEqual(len(legacy.publisher.database_rows(self.root / '.vimax')), 1)

    def test_independent_paused_contract_is_actual_and_failed_full_draft_keeps_errors(self):
        self.independent_fixture()
        self.engine.contract_errors = lambda source, run: ['real full draft contract error']
        with self.assertRaisesRegex(AssertionError, 'actual full-draft contracts'): self.invoke()
        value = json.loads(self.run_path.read_bytes()); value.update(status='failed', errors=['original program issue'], program_review_deferred=False)
        review_path = self.run_path.parent / 'review-index.json'; review = json.loads(review_path.read_bytes())
        review.update(program_review_deferred=False, verdict='needs_revision'); review_path.write_text(json.dumps(review), encoding='utf-8')
        hashes_path = self.run_path.parent / 'hashes.json'; hashes = json.loads(hashes_path.read_bytes())
        hashes['review-index.json'] = pub.sha(review_path.read_bytes()); hashes_path.write_text(json.dumps(hashes), encoding='utf-8')
        self.save_independent(value); self.invoke()
        actual = json.loads(legacy.publisher.database_rows(self.root / '.vimax')[value['id']])
        self.assertEqual(actual['status'], 'failed'); self.assertEqual(actual['errors'], ['original program issue'])
        self.assertEqual(actual['publication']['contract_errors'], ['real full draft contract error'])


class ArchiveSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(HERE / 'import-natural-dialogue.py', self.root / 'import-run.py')

    def bundle(self, extra=None, override=None):
        files = {'V01/run.json': b'original bytes', 'publish.py': b'frozen source'}
        manifest = {key: pub.sha(value) for key, value in files.items()}
        if override: files.update(override)
        path = self.root / ('input-' + str(len(list(self.root.glob('input*')))) + '.tar.gz')
        with tarfile.open(path, 'w:gz') as archive:
            for key, value in {**files, 'sha256.json': pub.canonical(manifest)}.items():
                member = tarfile.TarInfo(key); member.size = len(value); archive.addfile(member, io.BytesIO(value))
            if extra:
                member, value = extra; archive.addfile(member, io.BytesIO(value) if value is not None else None)
        return path, manifest

    def extract(self, path, manifest):
        return pub.extract_archive(path, self.root / 'archive', pub.sha(pub.canonical(manifest)), pub.sha(path.read_bytes()))

    def test_exact_member_sha_install_and_identical_retry_then_conflict_rejection(self):
        path, manifest = self.bundle(); self.assertEqual(self.extract(path, manifest), 'installed')
        self.assertEqual(self.extract(path, manifest), 'existing_identical')
        (self.root / 'archive/V01/run.json').write_bytes(b'tampered')
        with self.assertRaisesRegex(AssertionError, 'file set/SHA'): self.extract(path, manifest)
        self.assertEqual((self.root / 'archive/V01/run.json').read_bytes(), b'tampered')

    def test_bad_members_rejected_before_staging(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('V01/run.json', tarfile.REGTYPE), ('link', tarfile.SYMTYPE),
                           ('V01/RUN.JSON', tarfile.REGTYPE), ('extra', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                member = tarfile.TarInfo(name); member.type = kind; member.size = 0; member.linkname = '/etc/passwd'
                path, manifest = self.bundle((member, b'' if kind == tarfile.REGTYPE else None))
                with self.assertRaises(AssertionError): self.extract(path, manifest)
                self.assertFalse((self.root / 'archive').exists())
                self.assertFalse(list(self.root.glob('archive.staging-*')))

    def test_only_two_exact_audited_hidden_source_paths_are_allowed(self):
        for prefix in ('', 'V01/', 'V14-R001/', 'V40-R1234/'):
            for name in ('evolve-natural.py', 'generate-independent.py'):
                path = prefix + 'sources/.vimax/deploy/' + name
                self.assertEqual(pub.safe_name(path).as_posix(), path)
        for path in ('sources/.vimax/deploy/runtime-model.yaml', 'sources/.vimax/deploy/import-natural-dialogue.py',
                     '.vimax/deploy/evolve-natural.py', 'V01/sources/.private/deploy/evolve-natural.py',
                     'V01/sources/.vimax/deploy/evolve-natural.py/extra', 'V00/sources/.vimax/deploy/evolve-natural.py',
                     'V01-R001/sources/.vimax/../deploy/evolve-natural.py', 'V01/.runtime/app.sqlite3'):
            with self.subTest(path=path), self.assertRaises(AssertionError): pub.safe_name(path)

    def test_revision_prefixed_audited_source_bytes_survive_exact_tar_install(self):
        files = {'V14-R001/sources/.vimax/deploy/evolve-natural.py': b'complete archived base source\n',
                 'V14-R001/sources/.vimax/deploy/generate-independent.py': b'complete archived independent source\n'}
        manifest = {name: pub.sha(value) for name, value in files.items()}
        path = self.root / 'audited-sources.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, value in {**files, 'sha256.json': pub.canonical(manifest)}.items():
                member = tarfile.TarInfo(name); member.size = len(value); archive.addfile(member, io.BytesIO(value))
        self.assertEqual(self.extract(path, manifest), 'installed')
        for name, value in files.items(): self.assertEqual((self.root / 'archive' / name).read_bytes(), value)

    def test_bad_file_hash_or_bundle_hash_rejected_before_staging(self):
        path, manifest = self.bundle(override={'V01/run.json': b'wrong'})
        with self.assertRaisesRegex(AssertionError, 'Member SHA'): self.extract(path, manifest)
        with self.assertRaisesRegex(AssertionError, 'Bundle SHA'):
            pub.extract_archive(path, self.root / 'archive', pub.sha(pub.canonical(manifest)), '0' * 64)

    def test_host_publisher_parses_on_python36(self):
        ast.parse((HERE / 'publish-independent-safe.py').read_text(encoding='utf-8'), feature_version=(3, 6))

    def test_bulk_import_preserves_selected_order_twice_and_restores_argv(self):
        calls = []; saved = sys.argv[:]
        fake = types.SimpleNamespace(main=lambda: calls.append(sys.argv[:]))
        with patch.object(pub, 'load_module', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            pub.batch_import(self.root, self.root, ['V03', 'V01'], 'V01', 2)
        self.assertEqual([args[args.index('--version') + 1] for args in calls], ['V03', 'V01', 'V03', 'V01'])
        self.assertEqual([('--selected' in args) for args in calls], [False, True, False, True])
        self.assertEqual(sys.argv, saved)

    def test_transaction_hook_uses_one_container_for_both_candidate_passes(self):
        shutil.copyfile(HERE / 'publish-natural-safe.py', self.root / 'publish-natural-base.py')
        plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01', 'V02'], 'V02')
        base = pub.configure_base(plan, 'candidate', self.root)
        base.ARCHIVE = self.root / 'archive'; (base.ARCHIVE / 'corpus').mkdir(parents=True)
        (base.ARCHIVE / 'corpus/lccc.json').write_bytes(corpus_bytes())
        result = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(pub.subprocess, 'run', return_value=result) as command:
            base.import_runs(self.root / 'state', self.root / 'working')
            base.import_runs(self.root / 'state', self.root / 'working')
        self.assertEqual(command.call_count, 1)
        args = command.call_args.args[0]
        self.assertEqual(args[0:3], ['docker', 'run', '--rm'])
        self.assertEqual(args[args.index('--repeats') + 1], '2')
        self.assertEqual(base.BATCH, '20261015-abcdef0')
        self.assertEqual(Path(base.remote.__code__.co_filename), self.root / 'publish-natural-base.py')

    def test_no_selected_default_and_revision_labels_keep_literal_order(self):
        shutil.copyfile(HERE / 'publish-natural-safe.py', self.root / 'publish-natural-base.py')
        plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01', 'V01-R001'], None)
        base = pub.configure_base(plan, 'candidate', self.root)
        self.assertIsNone(base.SELECTED)
        calls = []; fake = types.SimpleNamespace(main=lambda: calls.append(sys.argv[:]))
        with patch.object(pub, 'load_module', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            pub.batch_import(self.root, self.root, plan['versions'], None, 2)
        self.assertFalse(any('--selected' in command for command in calls))
        self.assertEqual([command[command.index('--version') + 1] for command in calls], ['V01', 'V01-R001'] * 2)

    def test_runtime_corpus_atomic_install_retry_and_different_existing_is_preserved(self):
        state = self.root / 'state'
        self.assertTrue(pub.install_runtime_corpus(state, corpus_bytes()))
        self.assertFalse(pub.install_runtime_corpus(state, corpus_bytes()))
        target = state / 'dialogue/dialogue-corpus/lccc.json'
        target.write_bytes(b'previous user corpus')
        with self.assertRaisesRegex(AssertionError, 'refusing overwrite'):
            pub.install_runtime_corpus(state, corpus_bytes())
        self.assertEqual(target.read_bytes(), b'previous user corpus')

    def test_restore_hook_removes_only_this_transactions_new_runtime_corpus(self):
        for preexisting in (False, True):
            with self.subTest(preexisting=preexisting):
                state = self.root / ('state-' + str(preexisting)); events = []
                target = state / 'dialogue/dialogue-corpus/lccc.json'
                if preexisting: pub.install_runtime_corpus(state, corpus_bytes())
                fake = types.SimpleNamespace(STATE=state, configure=lambda *args: None,
                    verify_state=lambda *args: None, restore=lambda *args: events.append(target.exists()),
                    verify_runtime=lambda: None)
                plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
                with patch.object(pub, 'load_module', return_value=fake): base = pub.configure_base(plan, 'publish', self.root)
                base.ARCHIVE = self.root / 'archive-corpus'; (base.ARCHIVE / 'corpus').mkdir(parents=True, exist_ok=True)
                (base.ARCHIVE / 'corpus/lccc.json').write_bytes(corpus_bytes())
                with patch.object(pub.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout='', stderr='')):
                    base.import_runs(state, self.root / 'working')
                base.restore(self.root / 'snapshot', '/opt/vimax/releases/20261015-123abcd')
                self.assertEqual(events, [True]); self.assertEqual(target.exists(), preexisting)

    def test_independent_v01_verify_checks_human_model_and_reference_fields(self):
        shutil.copyfile(HERE / 'publish-natural-safe.py', self.root / 'publish-natural-base.py')
        plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
        base = pub.configure_base(plan, 'publish', self.root)
        source = evidence(legacy.run('V02')); source['program_review_history'] = [returned_review(source, {'goal_met': True, 'issues': []})]
        folder = self.root / 'archive/V01'; folder.mkdir(parents=True)
        (folder / 'run.json').write_text(json.dumps(source), encoding='utf-8')
        (folder / 'review-index.json').write_text('{"verdict":"candidate"}')
        (folder.parent / 'corpus').mkdir(); (folder.parent / 'corpus/lccc.json').write_bytes(corpus_bytes())
        base.ARCHIVE = folder.parent
        actual = json.loads(json.dumps(source)); actual['publication'] = {
            'series': pub.SERIES, 'parent_run': None, 'read_only': True,
            'source_run_sha256': pub.sha((folder / 'run.json').read_bytes()), 'selected': True, 'user_selected': True,
            'human_verdict': 'candidate', 'style_label': source['creative_variation']['style_label'],
            'program_review_state': 'reviewed'}
        state = self.root / 'state'; state.mkdir()
        pub.install_runtime_corpus(state, corpus_bytes())
        index = {'active_session_id': 'old', 'sessions': {}}
        (state / 'sessions.json').write_text(json.dumps(index))
        with patch.object(base, 'database_rows', side_effect=lambda state: {source['id']: json.dumps(actual)}):
            base.verify_state(state, {}, index)
            actual['publication']['human_verdict'] = 'alternative'
            with self.assertRaises(AssertionError): base.verify_state(state, {}, index)
            actual['publication']['human_verdict'] = 'candidate'; actual['corpus_reference'] = {'changed': True}
            with self.assertRaisesRegex(AssertionError, 'corpus_reference'): base.verify_state(state, {}, index)

    def test_publisher_verify_rejects_final_failed_call_mislabelled_as_reviewed(self):
        shutil.copyfile(HERE / 'publish-natural-safe.py', self.root / 'publish-natural-base.py')
        plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], None)
        base = pub.configure_base(plan, 'candidate', self.root)
        source = evidence(legacy.run('V02')); source.update(status='failed', errors=['final model review call failed'])
        good = returned_review(source, {'goal_met': True, 'issues': []})
        failed = {key: value for key, value in good.items() if key != 'review'}; failed.update(state='failed', error='connection failure')
        source.update(program_review_history=[good, failed], outcome_review={'met': True})
        folder = self.root / 'archive/V01'; folder.mkdir(parents=True)
        (folder / 'run.json').write_text(json.dumps(source), encoding='utf-8'); (folder / 'review-index.json').write_text('{"verdict":"needs_revision"}')
        (folder.parent / 'corpus').mkdir(); (folder.parent / 'corpus/lccc.json').write_bytes(corpus_bytes()); base.ARCHIVE = folder.parent
        actual = json.loads(json.dumps(source)); actual['publication'] = {'series': pub.SERIES, 'parent_run': None,
            'read_only': True, 'source_run_sha256': pub.sha((folder / 'run.json').read_bytes()), 'selected': False,
            'user_selected': False, 'human_verdict': 'needs_revision', 'style_label': source['creative_variation']['style_label'],
            'program_review_state': 'reviewed'}
        state = self.root / 'state'; state.mkdir(); pub.install_runtime_corpus(state, corpus_bytes())
        index = {'sessions': {}, 'active_session_id': 'old'}; (state / 'sessions.json').write_text(json.dumps(index))
        with patch.object(base, 'database_rows', side_effect=lambda state: {source['id']: json.dumps(actual)}):
            with self.assertRaises(AssertionError): base.verify_state(state, {}, index)
            actual['publication']['program_review_state'] = 'not_completed'; base.verify_state(state, {}, index)

    def source_tree(self):
        scripts = self.root / '.vimax/deploy'; scripts.mkdir(parents=True)
        for name in ('import-natural-dialogue.py', 'publish-natural-safe.py'):
            shutil.copyfile(HERE / name, scripts / name)
        output = self.root / 'outputs' / pub.SERIES; folder = output / 'V01'; folder.mkdir(parents=True)
        value = evidence(legacy.run('V02')); value['version_record'].update(version='V01')
        value['candidate_versions'] = [{'fixture': True}]
        (folder / 'run.json').write_text(json.dumps(value), encoding='utf-8')
        (folder / 'template.json').write_text('{}'); (folder / 'review-index.json').write_text('{}')
        hashes = {'run.json': pub.sha((folder / 'run.json').read_bytes())}
        for name in ('evolve-natural.py', 'generate-independent.py'):
            relative = 'sources/.vimax/deploy/' + name; path = folder / relative; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('complete source snapshot fixture: ' + name).encode())
            hashes[relative] = pub.sha(path.read_bytes())
        (folder / 'hashes.json').write_text(json.dumps(hashes))
        (output / 'versions.json').write_text(json.dumps([{'version': 'V01', 'run_id': value['id'], 'publication_eligible': True}]))
        (output / 'lccc.corpus.json').write_bytes(corpus_bytes())
        corpus = output / '语料来源/LCCC'; corpus.mkdir(parents=True)
        for name, text in [('metadata.json', '{}'), ('LICENSE.txt', 'MIT fixture'), ('conversations.json', '[["a","b"]]')]:
            (corpus / name).write_text(text)
        private = folder / '.runtime'; private.mkdir(); (private / 'app.sqlite3').write_text('SECRET')
        (folder / 'runtime-model.yaml').write_text('SECRET')
        return output

    def test_prepare_freezes_sources_corpus_and_filters_runtime_without_network(self):
        self.source_tree()
        with contextlib.redirect_stdout(io.StringIO()):
            directory = pub.prepare(self.root, '20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
        receipt = json.loads((directory / 'prepared.json').read_bytes()); names = set(receipt['manifest'])
        self.assertTrue({'publish.py', 'publish-natural-base.py', 'import-run.py', 'publication-plan.json'} <= names)
        self.assertIn('语料来源/LCCC/LICENSE.txt', names)
        self.assertIn('corpus/lccc.json', names)
        self.assertIn('V01/sources/.vimax/deploy/evolve-natural.py', names)
        self.assertIn('V01/sources/.vimax/deploy/generate-independent.py', names)
        self.assertFalse(any('.runtime' in name or 'runtime-model' in name for name in names))
        self.assertEqual(self.extract(directory / 'archive.tar.gz', receipt['manifest']), 'installed')

    def test_prepare_refuses_a_different_corpus_than_the_actual_run_used(self):
        output = self.source_tree(); path = output / 'V01/run.json'; run = json.loads(path.read_bytes())
        run['version_record']['corpus']['file_sha256'] = '0' * 64
        path.write_text(json.dumps(run), encoding='utf-8')
        with self.assertRaisesRegex(AssertionError, 'different runtime corpus'):
            pub.collect_files(self.root, pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], None))

    def test_unreviewed_registry_history_is_not_published_as_complete(self):
        output = self.source_tree(); path = output / 'versions.json'; records = json.loads(path.read_bytes())
        records[0]['publication_eligible'] = False; path.write_text(json.dumps(records))
        with self.assertRaisesRegex(AssertionError, 'not publication-eligible'):
            pub.collect_files(self.root, pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], None))

    def test_upload_sends_exactly_one_tar_with_fake_ssh_then_full_verify(self):
        self.source_tree()
        with contextlib.redirect_stdout(io.StringIO()):
            pub.prepare(self.root, '20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
        transfers, commands = [], []
        class SFTP:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def file(self, *args): raise FileNotFoundError()
            def put(self, local, remote): transfers.append((local, remote))
            def chmod(self, path, mode): pass
        class Client:
            def load_system_host_keys(self): pass
            def set_missing_host_key_policy(self, policy): pass
            def connect(self, *args, **kwargs): pass
            def open_sftp(self): return SFTP()
            def close(self): pass
        fake = types.SimpleNamespace(SSHClient=Client, RejectPolicy=lambda: None)
        plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
        with patch.dict(sys.modules, {'paramiko': fake}), patch.dict(os.environ, {'VIMAX_TASK_SSH_PASSWORD': 'fake-only'}), \
             patch.object(pub, 'ssh_run', side_effect=lambda client, command, password: commands.append(command)):
            pub.upload('prestage', self.root, plan, 'fixture.invalid', 22, 'fixture')
        self.assertEqual(len(transfers), 1)
        self.assertTrue(transfers[0][1].startswith('/opt/vimax/shared/state/dialogue-versions/20261015-abcdef0.upload-'))
        self.assertIn('readlink -f /opt/vimax/current', commands[0])
        self.assertIn('extract_archive', commands[1]); self.assertIn('natural-publish.lock', commands[1])
        self.assertIn('verify', commands[2]); self.assertNotIn('candidate', commands[2])

    def test_existing_identical_manifest_skips_upload_but_conflict_never_overwrites(self):
        self.source_tree()
        with contextlib.redirect_stdout(io.StringIO()):
            directory = pub.prepare(self.root, '20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
        manifest = json.loads((directory / 'prepared.json').read_bytes())['manifest']
        commands, transfers = [], []
        existing = [manifest]
        class SFTP:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def file(self, *args): return io.BytesIO(pub.canonical(existing[0]))
            def put(self, *args): transfers.append(args)
        class Client:
            def load_system_host_keys(self): pass
            def set_missing_host_key_policy(self, policy): pass
            def connect(self, *args, **kwargs): pass
            def open_sftp(self): return SFTP()
            def close(self): pass
        fake = types.SimpleNamespace(SSHClient=Client, RejectPolicy=lambda: None)
        plan = pub.arguments('20261015-abcdef0', '20261015-123abcd', ['V01'], 'V01')
        with patch.dict(sys.modules, {'paramiko': fake}), patch.dict(os.environ, {'VIMAX_TASK_SSH_PASSWORD': 'fake-only'}), \
             patch.object(pub, 'ssh_run', side_effect=lambda client, command, password: commands.append(command)):
            pub.upload('prestage', self.root, plan, 'fixture.invalid', 22, 'fixture')
            self.assertFalse(transfers); self.assertEqual(len(commands), 2); self.assertIn('verify', commands[-1])
            existing[0] = {'different': '0' * 64}
            with self.assertRaisesRegex(AssertionError, 'Immutable archive differs'):
                pub.upload('prestage', self.root, plan, 'fixture.invalid', 22, 'fixture')
            self.assertFalse(transfers)


if __name__ == '__main__': unittest.main(verbosity=2)
