"""Offline registration tests; every purported model/human datum is a fixture."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('independent_register_tested', HERE / 'register-independent.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class IndependentRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='independent-registry-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '独立随机-20261015'
        self.root.mkdir()

    def save(self, path, value):
        driver.base.save_json(path, value)

    def rehash(self, folder):
        self.save(folder / 'hashes.json', {path.relative_to(folder).as_posix(): driver.digest(path)
                  for path in folder.rglob('*') if path.is_file() and path.name != 'hashes.json'})

    def fixture(self, index=1, attempt_label=None, count=150, passed=True):
        label = driver.label_for(index, attempt_label)
        folder = self.root / label
        folder.mkdir()
        messages = [{'seq': i, 'sender': '坐席' if i == 1 else '客户1', 'content': '离线虚构测试句' + str(i),
                     'reply_to': None, 'delay': '3', 'stage': '自然群聊', 'type': '文本'} for i in range(1, count + 1)]
        fingerprint = driver.canonical_hash(messages)
        template = {'source': {'messages': [{'content': '仅母本测试'}]}, 'analysis': {'confirmed': True}}
        self.save(folder / 'template.json', template)
        driver.base.atomic(folder / 'sources/dialogue/backend/app/fresh.py', b'# offline source fixture\n')
        provenance = {'commit': 'offline-test-commit', 'source_hashes': {
            'dialogue/backend/app/fresh.py': driver.digest(folder / 'sources/dialogue/backend/app/fresh.py')}}
        self.save(folder / 'provenance.json', provenance)
        cards = {'坐席': {'identity': '坐席'}, '客户1': {'identity': '离线人物'}}
        config = {'target_messages': 150, 'story_world': label + '-fresh-world', 'variation_seed': label + '-seed',
                  'persona_cards': cards, 'creation_mode': 'independent_random', 'dialogue_model': 'gpt-6-sol', 'reasoning_effort': 'high'}
        record = {'series': driver.SERIES, 'version': f'V{index:02d}', 'method': 'independent_generation',
                  'parent_run': None, 'parent_sha': None, 'parent_sha256': None, 'seed': config['variation_seed'],
                  'code': provenance['commit'], 'provenance': provenance,
                  'template_sha256': driver.digest(folder / 'template.json'), 'counts_as_new_content': passed}
        corpus_source = {'url': 'https://example.invalid/corpus', 'license': 'offline fixture'}
        record['corpus'] = {'file_sha256': 'a' * 64, 'conversations': 1000, 'sources': [corpus_source]}
        if attempt_label:
            record['revision'] = attempt_label
        history = [{'state': 'returned', 'model': 'gpt-6-sol', 'reasoning_effort': 'high',
                    'messages_sha256': fingerprint, 'message_count': count,
                    'review': {'issues': [], 'goal_met': True}}] if passed else []
        samples = [{'purpose': 'planning', 'content': 'offline raw planning fixture'},
                   {'purpose': 'writer', 'content': 'offline raw writer fixture'}]
        run = {'id': f'{index:028x}' + (f'{int(attempt_label[1:]):04x}' if attempt_label else '0000'), 'status': 'completed' if passed else 'failed',
               'version_record': record, 'messages': messages, 'config': config, 'errors': [] if passed else ['离线失败'],
               'quality_review': [], 'program_review_history': history, 'vimax_samples': samples,
               'independence': {'method': 'independent_generation', 'parent_run': None, 'initial_messages': 0,
                                'initial_story': None, 'fresh_story_world': config['story_world']},
               'creative_variation': {'method': 'independent_generation', 'parent_run': None,
                                      'seed': config['variation_seed'], 'persona_cards': cards, 'style_label': '测试随机风格'},
               'corpus_reference': {'samples': [{'id': 'fixture-sample'}], 'sources': [corpus_source]},
               'candidate_versions': []}
        if count == 150:
            snapshot_name = 'R000-' + fingerprint[:8]
            snapshot = {'hash': fingerprint, 'path': f"versions/{run['id']}/{snapshot_name}", 'version': 'R000'}
            run['candidate_versions'] = [snapshot]
            directory = folder / 'snapshots' / snapshot_name
            self.save(directory / 'run.json', run)
            driver.base.atomic(directory / 'messages.md', b'offline original draft')
            driver.base.atomic(directory / 'messages.xlsx', b'offline xlsx bytes')
            self.save(directory / 'manifest.json', {'record': snapshot, 'message_count': 150,
                      'files': {name: driver.digest(directory / name) for name in ('run.json', 'messages.md', 'messages.xlsx')}})
        self.save(folder / 'run.json', run)
        self.save(folder / 'review-calls.json', history)
        self.save(folder / 'generation-report.json', {'series': driver.SERIES, 'version': f'V{index:02d}',
                  'attempt_label': attempt_label, 'id': run['id'], 'status': run['status'], 'messages': count,
                  'messages_sha256': fingerprint, 'actual_full_review_passed': passed, 'success': passed})
        self.save(folder / 'input.json', {'series': driver.SERIES, 'index': index, 'seed': record['seed'],
                  'attempt_label': attempt_label, 'template_sha256': record['template_sha256'], 'corpus': record['corpus']})
        for number, sample in enumerate(samples, 1):
            self.save(folder / f'attempts/call-{number:03d}.json', sample)
        self.rehash(folder)
        return folder, run

    def reports(self, folder, run, **changes):
        fingerprint = driver.canonical_hash(run['messages'])
        reviews = {}
        for role, filename in driver.REPORTS.items():
            path = folder / 'reviews' / filename
            driver.base.atomic(path, f'离线报告夹具，非实际读稿结论。消息SHA256：{fingerprint}'.encode())
            reviews[role] = {'full_read': True, 'message_count': 150, 'reviewer_kind': 'agent',
                             'reviewer_id': role + '-offline-fixture', 'report': 'reviews/' + filename,
                             'report_sha256': driver.digest(path)}
        evidence = {'series': driver.SERIES, 'version': folder.name, 'run_sha256': driver.digest(folder / 'run.json'),
                    'message_sha256': fingerprint, 'reviews': reviews}
        evidence.update(changes)
        self.save(folder / 'reviews/review-evidence.json', evidence)
        return evidence

    def test_registration_does_not_infer_agent_acceptance_or_user_approval_from_model_success(self):
        folder, run = self.fixture()
        entry = driver.register(1, summary='模型已完成，尚未双人审读', root=self.root)
        self.assertTrue(entry['model_passed'])
        self.assertFalse(entry['dual_full_read'])
        self.assertEqual(entry['human_verdict'], 'unreviewed')
        self.assertEqual(entry['user_approval'], 'unset')
        self.assertFalse(entry['publication_eligible'])
        self.assertFalse((folder / 'review-index.json').exists())
        self.assertEqual(driver.register(1, summary='模型已完成，尚未双人审读', root=self.root), entry)
        counts = driver.read_json(self.root / 'independent-registry.json')['counts']
        self.assertEqual(counts['model_passed_indices'], 1)
        self.assertEqual(counts['dual_reviewed_indices'], 0)
        self.assertEqual(counts['recommended_indices'], 0)

    def test_dual_reports_bound_to_exact_final_run_and_importer_shape(self):
        folder, run = self.fixture()
        self.reports(folder, run)
        entry = driver.register(1, verdict='alternative', summary='离线夹具的明确备选分类', root=self.root)
        review = driver.read_json(folder / 'review-index.json')
        self.assertEqual(review['reports'], ['V01/reviews/R000-primary-review.md', 'V01/reviews/R000-subagent-review.md'])
        self.assertTrue(review['dual_full_read'])
        self.assertEqual(review['reviewer_kind'], 'agent')
        self.assertEqual(review['user_approval'], 'unset')
        self.assertEqual(entry['run_sha256'], driver.digest(folder / 'run.json'))
        hashes = driver.read_json(folder / 'hashes.json')
        self.assertEqual(hashes['review-index.json'], driver.digest(folder / 'review-index.json'))
        for name in driver.REPORTS.values():
            self.assertEqual(hashes['reviews/' + name], driver.digest(folder / 'reviews' / name))
        self.assertFalse(entry['recommended'])

    def test_attempts_count_once_per_index_and_failed_partial_payload_stays_registered(self):
        for index, attempt, count, passed in [(1, None, 0, False), (1, 'R001', 37, False),
                                               (1, 'R002', 150, True), (4, None, 150, True)]:
            folder, run = self.fixture(index, attempt, count, passed)
            before = (folder / 'run.json').read_bytes()
            driver.register(index, attempt, summary='离线attempt记录', root=self.root)
            self.assertEqual((folder / 'run.json').read_bytes(), before)
        registry = driver.read_json(self.root / 'independent-registry.json')
        self.assertEqual(registry['counts']['registered_attempts'], 4)
        self.assertEqual(registry['counts']['registered_indices'], 2)
        self.assertEqual(registry['counts']['model_passed_indices'], 2)
        self.assertFalse(registry['counts']['all_40_model_passed'])

    def test_cannot_claim_full_read_from_hash_only_files_or_same_reviewer(self):
        folder, run = self.fixture()
        evidence = self.reports(folder, run)
        for modify in ('partial', 'same_reviewer', 'wrong_run', 'wrong_kind'):
            bad = copy.deepcopy(evidence)
            if modify == 'partial': bad['reviews']['subagent']['full_read'] = False
            if modify == 'same_reviewer': bad['reviews']['subagent']['reviewer_id'] = bad['reviews']['main']['reviewer_id']
            if modify == 'wrong_run': bad['run_sha256'] = '0' * 64
            if modify == 'wrong_kind': bad['reviews']['main']['reviewer_kind'] = 'human_user'
            self.save(folder / 'reviews/review-evidence.json', bad)
            with self.subTest(modify=modify), self.assertRaises(ValueError):
                driver.register(1, verdict='candidate', summary='错误证明应拒绝', root=self.root)
        self.assertFalse((self.root / 'versions.json').exists())

    def test_generation_raw_provenance_and_snapshots_cannot_be_changed(self):
        for index, relative in enumerate(('run.json', 'generation-report.json', 'review-calls.json',
                                          'provenance.json', 'sources/dialogue/backend/app/fresh.py',
                                          'attempts/call-001.json'), 1):
            folder, run = self.fixture(index)
            path = folder / relative
            driver.base.atomic(path, path.read_bytes() + b' ')
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, 'SHA'):
                driver.register(index, summary='损坏证据不能登记', root=self.root)

    def test_consistently_rehashed_but_semantically_mismatched_raw_and_review_are_rejected(self):
        folder, run = self.fixture()
        self.save(folder / 'attempts/call-001.json', {'content': 'fake replacement'})
        self.rehash(folder)
        with self.assertRaisesRegex(ValueError, '原始响应'):
            driver.register(1, summary='错误原始记录', root=self.root)
        self.save(folder / 'attempts/call-001.json', run['vimax_samples'][0])
        self.save(folder / 'review-calls.json', [])
        self.rehash(folder)
        with self.assertRaisesRegex(ValueError, '审查调用'):
            driver.register(1, summary='错误审查记录', root=self.root)

    def test_series_identity_and_path_escape_rejected(self):
        folder, run = self.fixture()
        run['version_record']['series'] = '自然群聊-20261015'
        self.save(folder / 'run.json', run)
        with self.assertRaisesRegex(ValueError, '独立随机'):
            driver.register(1, summary='不能混入旧系列', root=self.root)
        hashes = driver.read_json(folder / 'hashes.json')
        hashes['../outside'] = '0' * 64
        self.save(folder / 'hashes.json', hashes)
        run['version_record']['series'] = driver.SERIES
        self.save(folder / 'run.json', run)
        hashes['run.json'] = driver.digest(folder / 'run.json')
        self.save(folder / 'hashes.json', hashes)
        with self.assertRaisesRegex(ValueError, '越出'):
            driver.register(1, summary='不能越界', root=self.root)

    def test_explicit_reassessment_appends_event_and_keeps_prior_evidence_frozen(self):
        folder, run = self.fixture()
        driver.register(1, summary='暂未审读', root=self.root)
        original_event = next((folder / 'registrations').iterdir())
        original_bytes = original_event.read_bytes()
        self.reports(folder, run)
        with self.assertRaisesRegex(ValueError, 'reassess'):
            driver.register(1, verdict='candidate', summary='离线明确新评估', root=self.root)
        entry = driver.register(1, verdict='candidate', summary='离线明确新评估', reassess=True, root=self.root)
        self.assertEqual(original_event.read_bytes(), original_bytes)
        self.assertEqual(len(list((folder / 'registrations').iterdir())), 2)
        self.assertTrue(entry['dual_full_read'])
        self.assertEqual(entry['user_approval'], 'unset')
        report = folder / 'reviews/R000-subagent-review.md'
        driver.base.atomic(report, report.read_bytes() + b' altered')
        self.rehash(folder)
        with self.assertRaisesRegex(ValueError, '冻结'):
            driver.register(1, verdict='candidate', summary='离线明确新评估', root=self.root)


if __name__ == '__main__':
    unittest.main()
