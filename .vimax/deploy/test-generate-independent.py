"""Offline fresh-generation integration: real API/SQLite/archive, fake worker/review."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import httpx


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('independent_driver_tested', HERE / 'generate-independent.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
SANDBOX = tempfile.TemporaryDirectory(prefix='independent-generation-offline-')
os.environ.update(SCRIPT_GENERATOR_DATA_DIR=str(Path(SANDBOX.name) / 'initial-runtime'),
                  MODEL_API_BASE_URL='https://example.invalid/v1', MODEL_API_KEY='offline-only',
                  MODEL_NAME='gpt-6-sol', MODEL_DIALOGUE_NAME='gpt-6-sol', MODEL_REVIEW_NAME='gpt-6-sol')
sys.path[:0] = [str(driver.base.ROOT), str(driver.base.ROOT / 'dialogue/backend')]
from app import main as api
from app import vimax_engine as engine
from app.dialogue_corpus import load_corpus, parse_lccc, sample_reference, save_corpus


class IndependentGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='independent-case-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.corpus_path = self.root / 'lccc.corpus.json'
        save_corpus(parse_lccc([["周末去看电影么", "还没想好呢"], ["下雨了吗", "刚停雨"],
                               ["想去看看海", "我喜欢听海浪"], ["今天就想偷个懒", "我也是"]],
                    {'name': 'offline-fixture', 'url': 'https://example.invalid/corpus', 'license': 'test fixture'}), self.corpus_path)
        self.initial = []
        self.worker_done = False
        self.original_read = Path.read_bytes
        self.forbidden_reads = set()

    def configure(self, out):
        self.runtime = out / '.runtime'
        self.runtime.mkdir(parents=True)
        api.DATA_DIR, api.DB_PATH, api.CORPUS_PATH = self.runtime, self.runtime / 'app.sqlite3', self.corpus_path
        api.init_db()

    def guard_read(self, path):
        if path.resolve() in self.forbidden_reads:
            raise AssertionError('same-index previous attempt read attempted')
        if path.name in {'run.json', 'messages.json', 'story.json'} and '自然群聊-20261015' in str(path):
            raise AssertionError('old dialogue read attempted')
        return self.original_read(path)

    def args(self, index=1, seed='offline-seed', customer_count=None, attempt_label=None, material_notes=None,
             allow_review_done_without_stop=False):
        return SimpleNamespace(index=index, seed=seed, customer_count=customer_count, attempt_label=attempt_label,
                               template=driver.DEFAULT_TEMPLATE, corpus_path=self.corpus_path, material_notes=material_notes,
                               allow_review_done_without_stop=allow_review_done_without_stop)

    def notes_fixture(self, index=1, seed='offline-seed'):
        corpus = load_corpus(self.corpus_path)
        corpus_sha = driver.base.sha(self.corpus_path.read_bytes())
        samples = sample_reference(corpus, seed, count=4, max_turns=4)['samples']
        sources = {source['source_id']: source for source in corpus['sources']}
        entry = {'version': f'V{index:02d}', 'seed': seed, 'annotation_kind': 'agent_creative_inspiration',
                 'read_selected_excerpts': True, 'corpus_sha256': corpus_sha,
                 'conversation_ids': [sample['id'] for sample in samples], 'reference_bindings': [],
                 'material_hints': [{'topic_hint': '海边的声音', 'creative_prompt': '惦记海浪声，不一定要马上出门。',
                                    'expression_impulse': '表达向往', 'inspired_by_conversation_ids': [samples[0]['id']]},
                                   {'topic_hint': '偷懒的快乐', 'creative_prompt': '有时只想轻轻松松待一会儿。',
                                    'expression_impulse': '分享心情', 'inspired_by_conversation_ids': [samples[1]['id']]}]}
        for sample in samples:
            entry['reference_bindings'].append({'conversation_id': sample['id'],
                'conversation_content_sha256': sample['content_sha256'], 'excerpt_start': sample['excerpt_start'],
                'source_turn_indexes': [turn['source_turn_index'] for turn in sample['turns']],
                'source_sha256_bindings': [{'source_id': a['source_id'], 'source_record_index': a['source_record_index'],
                    'source_sha256': sources[a['source_id']]['source_sha256']} for a in sample['attributions']]})
        document = {'schema_version': 1, 'annotation_kind': 'agent_creative_inspiration',
                    'not_original_dialogue': True, 'corpus_sha256': corpus_sha, 'entries': [entry]}
        return document, corpus

    def write_notes(self, document):
        path = self.root / 'seed-material-notes.json'
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return path

    async def review(self, template, run):
        self.assertEqual(len(run['messages']), 150)
        await asyncio.sleep(0)
        return {'issues': [], 'goal_met': True, 'goal_reason': 'offline mocked review response'}

    def worker(self, mode='pass'):
        async def process(session_id):
            self.assertFalse(api._run_allow_done_without_stop.get())  # Writing/planning stay strict.
            run = api.get_run(session_id)
            self.initial.append(copy.deepcopy(run))
            self.assertFalse(run['messages'])
            self.assertFalse(run.get('vimax_story'))
            self.assertFalse(run.get('person_situations'))
            self.assertTrue(run['config']['story_world'].startswith('independent-'))
            # bind_characters has now allocated fresh IDs in the new world;
            # the submitted SimulationConfig was empty, not these persisted IDs.
            self.assertEqual(set(run['config']['character_ids']), set(run['agent_sessions']))
            self.assertTrue(run['corpus_reference'])
            self.assertTrue(all(not state['memory'] and not state['turns'] for state in run['agent_sessions'].values()))
            await asyncio.sleep(.01)
            count = {'zero_failure': 0, 'partial_failure': 37}.get(mode, 150)
            run['messages'] = [{'seq': i, 'sender': '坐席' if i == 1 else '客户1', 'content': f'离线测试台词{i}',
                                'reply_to': None, 'delay': '4', 'stage': '自然群聊', 'type': '文本',
                                'image_code': None, 'image_mode': None, 'replace_date': None,
                                'performed_steps': []} for i in range(1, count + 1)]
            run['cursor'] = count
            api.save_run(run)
            if mode in {'zero_failure', 'partial_failure', 'complete_failure'}:
                raise RuntimeError('offline injected task failure')
            if mode != 'fake_completed_without_review':
                engine.checkpoint_candidate(api, run, run['messages'], 'offline actual archive integration')
                result = await api.review_dialogue(api.get_template(run['template_id']), run)
                if mode == 'repair_twice':
                    run['messages'][9]['content'] += '，修复了具体问题'
                    api.save_run(run)
                    engine.checkpoint_candidate(api, run, run['messages'], 'offline repair candidate retained')
                    result = await api.review_dialogue(api.get_template(run['template_id']), run)
                self.assertFalse(api._run_allow_done_without_stop.get())
                run['quality_review'] = result['issues']
                run['outcome_review'] = {'met': result['goal_met'], 'reason': result['goal_reason']}
                if mode == 'review_then_drift':
                    run['messages'][4]['content'] += '，审查后不允许无记录改写'
            run['status'] = 'completed'
            api.save_run(run)
            self.worker_done = True
        return process

    async def invoke(self, args=None, mode='pass', review=None):
        original_create = Mock(side_effect=AssertionError('global SessionIndex create_run called'))
        captured_counts = []
        capture = driver.base.capture_artifacts
        def real_capture(api_arg, out, run, export):
            captured_counts.append(len(run['messages']))
            return capture(api_arg, out, run, export)
        def guarded(path):
            return self.guard_read(path)
        with patch.object(driver, 'OUT_ROOT', self.root / 'outputs/独立随机-20261015'), \
             patch.object(driver.base, 'configure_environment', self.configure), \
             patch.object(driver.base, 'capture_artifacts', real_capture), \
             patch.object(api, 'create_run', original_create), \
             patch.object(api, 'process_run', self.worker(mode)), \
             patch.object(api, 'review_dialogue', review or self.review), \
             patch.object(Path, 'read_bytes', guarded):
            code = await driver.execute(args or self.args())
            self.assertIs(api.create_run, original_create)
            original_create.assert_not_called()
        return code, captured_counts

    async def test_actual_api_new_world_empty_input_task_completion_sources_and_review_archive(self):
        code, counts = await self.invoke()
        out = self.root / 'outputs/独立随机-20261015/V01'
        run = json.loads((out / 'run.json').read_bytes())
        self.assertEqual(code, 0)
        self.assertTrue(self.worker_done)
        self.assertIn(0, counts)
        self.assertEqual(counts[-1], 150)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['version_record']['method'], 'independent_generation')
        self.assertEqual(run['version_record']['series'], '独立随机-20261015')
        self.assertIsNone(run['version_record']['parent_run'])
        self.assertIsNone(run['version_record']['parent_sha'])
        self.assertEqual(run['config']['creation_mode'], 'independent_random')
        self.assertEqual(run['config']['variation_seed'], 'offline-seed')
        self.assertEqual(run['config']['dialogue_model'], 'gpt-6-sol')
        self.assertEqual(run['config']['reasoning_effort'], 'high')
        self.assertEqual(run['config']['target_messages'], 150)
        self.assertEqual(run['config']['story_notes'], '')
        self.assertNotIn('material_notes', run['version_record'])
        self.assertFalse((out / 'material-notes.json').exists())
        self.assertTrue(run['config']['randomize_personas'])
        self.assertEqual(run['program_review_history'][0]['messages_sha256'], driver.messages_hash(run['messages']))
        self.assertEqual(run['program_review_history'][0]['state'], 'returned')
        self.assertTrue((out / '群聊150条.xlsx').is_file())
        self.assertTrue((out / '群聊150条.md').is_file())
        self.assertEqual(len(list((out / 'snapshots').iterdir())), 1)
        provenance = json.loads((out / 'provenance.json').read_bytes())
        for relative, digest in provenance['source_hashes'].items():
            self.assertEqual(driver.base.sha((out / 'sources' / relative).read_bytes()), digest)
        self.assertIn('.vimax/deploy/generate-independent.py', provenance['source_hashes'])
        self.assertEqual((out / 'template.json').read_bytes(), driver.DEFAULT_TEMPLATE.read_bytes())
        self.assertFalse(run['version_record']['review_stream_policy']['allow_done_without_stop_strict_json'])
        self.assertFalse(run['program_review_history'][0]['stream_policy']['allow_done_without_stop_strict_json'])

    async def test_missing_stop_policy_is_explicit_review_only_and_archives_actual_protocol(self):
        requests = []
        review_body = {'issues': [], 'goal_met': True, 'goal_reason': 'offline complete review'}
        text = ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {
            'content': json.dumps(review_body)}, 'finish_reason': None}]}) + '\n\ndata: [DONE]\n\n').encode()
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, content=text)
        async def streamed_review(template, run):
            self.assertTrue(api._run_allow_done_without_stop.get())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                token = api._run_model_client.set(client)
                try:
                    return await api.model_json('Offline review', {}, stream=True, call_budget_seconds=.4)
                finally:
                    api._run_model_client.reset(token)
        code, _ = await self.invoke(self.args(allow_review_done_without_stop=True), review=streamed_review)
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 1)
        self.assertFalse(api._run_allow_done_without_stop.get())
        self.assertIsNone(api._run_stream_protocol_records.get())
        out = self.root / 'outputs/独立随机-20261015/V01'
        run = json.loads((out / 'run.json').read_bytes())
        history = run['program_review_history'][0]
        self.assertTrue(history['stream_policy']['allow_done_without_stop_strict_json'])
        self.assertEqual(history['stream_protocol'], [{'model': 'gpt-6-sol', 'stop_received': False,
            'done_received': True, 'allow_done_without_stop': True, 'compatibility_used': True,
            'strict_json_validated': True, 'json_type': 'object', 'json_fence': False}])
        self.assertEqual(history, json.loads((out / 'review-calls.json').read_bytes())[0])
        self.assertTrue(json.loads((out / 'input.json').read_bytes())['review_stream_policy']['allow_done_without_stop_strict_json'])

    async def test_missing_stop_review_policy_is_reset_on_failure(self):
        async def failed_review(template, run):
            self.assertTrue(api._run_allow_done_without_stop.get())
            raise RuntimeError('offline stream failure')
        code, _ = await self.invoke(self.args(allow_review_done_without_stop=True), review=failed_review)
        self.assertEqual(code, 1)
        self.assertFalse(api._run_allow_done_without_stop.get())
        self.assertIsNone(api._run_stream_protocol_records.get())

    async def test_bound_agent_notes_drive_only_fresh_story_notes_and_are_archived_verbatim(self):
        document, _ = self.notes_fixture()
        path = self.write_notes(document)
        original_bytes = path.read_bytes()
        code, _ = await self.invoke(self.args(material_notes=path))
        self.assertEqual(code, 0)
        out = self.root / 'outputs/独立随机-20261015/V01'
        run = json.loads((out / 'run.json').read_bytes())
        archive = json.loads((out / 'material-notes.json').read_bytes())
        self.assertEqual(archive['selected_entry'], document['entries'][0])
        self.assertEqual(archive['source_file_sha256'], driver.base.sha(original_bytes))
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertIn('Agent创作启发，非原语料/当前人物事实', self.initial[0]['config']['story_notes'])
        self.assertIn('惦记海浪声', run['config']['story_notes'])
        self.assertNotIn('reference_bindings', run['config']['story_notes'])
        self.assertIn('不规定事件顺序、操作者', run['config']['story_notes'])
        self.assertEqual(run['version_record']['material_notes'], json.loads((out / 'input.json').read_bytes())['material_notes'])
        self.assertEqual(run['version_record']['material_notes']['source_file_sha256'], driver.base.sha(original_bytes))
        self.assertEqual(run['corpus_reference']['seed'], 'offline-seed')
        self.assertEqual([s['id'] for s in run['corpus_reference']['samples']], document['entries'][0]['conversation_ids'])
        self.assertIn('material-notes.json', json.loads((out / 'hashes.json').read_bytes()))

    def test_material_notes_reject_misbound_seed_sha_ids_missing_fields_and_duplicates(self):
        original, corpus = self.notes_fixture()
        mutations = [
            lambda d: d['entries'][0].update(seed='wrong-seed'),
            lambda d: d.update(corpus_sha256='wrong-sha'),
            lambda d: d['entries'][0].update(corpus_sha256='wrong-sha'),
            lambda d: d['entries'][0]['conversation_ids'].__setitem__(0, 'wrong-id'),
            lambda d: d['entries'][0]['reference_bindings'][0]['source_sha256_bindings'][0].update(source_sha256='wrong-sha'),
            lambda d: d['entries'][0].pop('reference_bindings'),
            lambda d: d['entries'][0]['material_hints'][0].pop('expression_impulse'),
            lambda d: d['entries'][0]['material_hints'][0].update(inspired_by_conversation_ids=['foreign-id']),
            lambda d: d.update(annotation_kind='original_dialogue'),
            lambda d: d.pop('not_original_dialogue'),
            lambda d: d['entries'].append(copy.deepcopy(d['entries'][0])),
        ]
        for index, mutate in enumerate(mutations):
            document = copy.deepcopy(original)
            mutate(document)
            with self.subTest(case=index), self.assertRaises(ValueError):
                driver.read_material_notes(self.write_notes(document), 1, 'offline-seed', corpus,
                                           driver.base.sha(self.corpus_path.read_bytes()))

    async def test_material_binding_failure_never_starts_a_generation_task(self):
        document, _ = self.notes_fixture()
        document['entries'][0]['seed'] = 'wrong-seed'
        code, _ = await self.invoke(self.args(material_notes=self.write_notes(document)))
        self.assertEqual(code, 1)
        self.assertFalse(self.initial)
        out = self.root / 'outputs/独立随机-20261015/V01'
        self.assertFalse((out / 'run-id.txt').exists())
        self.assertEqual(json.loads((out / 'failure.json').read_bytes())['phase'], 'setup')

    async def test_two_indices_create_distinct_worlds_and_do_not_overwrite(self):
        await self.invoke(self.args(1, 'first'))
        await self.invoke(self.args(2, 'second'))
        self.assertNotEqual(self.initial[0]['id'], self.initial[1]['id'])
        self.assertNotEqual(self.initial[0]['config']['story_world'], self.initial[1]['config']['story_world'])
        self.assertNotEqual(self.initial[0]['creative_variation'], self.initial[1]['creative_variation'])
        out = self.root / 'outputs/独立随机-20261015/V01/run.json'
        previous = out.read_bytes()
        with self.assertRaises(FileExistsError):
            await self.invoke(self.args(1, 'replacement'))
        self.assertEqual(out.read_bytes(), previous)

    async def test_same_index_attempt_is_fresh_and_same_seed_does_not_reuse_old_run(self):
        await self.invoke(self.args(1, 'same-seed'))
        original = self.root / 'outputs/独立随机-20261015/V01/run.json'
        original_bytes = original.read_bytes()
        self.forbidden_reads.add(original.resolve())
        code, _ = await self.invoke(self.args(1, 'same-seed', attempt_label='R001'))
        run = json.loads((self.root / 'outputs/独立随机-20261015/V01-R001/run.json').read_bytes())
        self.assertEqual(code, 0)
        self.assertEqual(run['version_record']['version'], 'V01')
        self.assertEqual(run['version_record']['revision'], 'R001')
        self.assertIsNone(run['version_record']['parent_run'])
        self.assertEqual(self.initial[0]['creative_variation'], self.initial[1]['creative_variation'])
        self.assertNotEqual(self.initial[0]['id'], self.initial[1]['id'])
        self.assertNotEqual(self.initial[0]['config']['story_world'], self.initial[1]['config']['story_world'])
        self.assertEqual(original.read_bytes(), original_bytes)
        with self.assertRaises(FileExistsError):
            await self.invoke(self.args(1, 'different-seed', attempt_label='R001'))
        with self.assertRaises(ValueError):
            await self.invoke(self.args(1, 'new', attempt_label='R000'))

    async def test_zero_partial_and_full_failures_are_retained_and_not_counted(self):
        for index, mode, count in [(1, 'zero_failure', 0), (2, 'partial_failure', 37), (3, 'complete_failure', 150)]:
            with self.subTest(mode=mode):
                code, _ = await self.invoke(self.args(index), mode=mode)
                out = self.root / f'outputs/独立随机-20261015/V{index:02d}'
                run = json.loads((out / 'run.json').read_bytes())
                self.assertEqual(code, 1)
                self.assertEqual(len(run['messages']), count)
                self.assertEqual(run['status'], 'failed')
                self.assertFalse(run['version_record']['counts_as_new_content'])
                self.assertFalse(json.loads((out / 'generation-report.json').read_bytes())['success'])

    async def test_completed_without_actual_review_is_rejected(self):
        code, _ = await self.invoke(mode='fake_completed_without_review')
        self.assertEqual(code, 1)
        run = json.loads((self.root / 'outputs/独立随机-20261015/V01/run.json').read_bytes())
        self.assertEqual(run['program_review_history'], [])
        self.assertEqual(run['status'], 'failed')

    async def test_real_review_exception_is_recorded_and_actual_full_candidate_is_retained(self):
        async def failed_review(*args):
            raise RuntimeError('offline-only https://example.invalid/v1 failed')
        code, _ = await self.invoke(review=failed_review)
        run = json.loads((self.root / 'outputs/独立随机-20261015/V01/run.json').read_bytes())
        self.assertEqual(code, 1)
        self.assertEqual(len(run['messages']), 150)
        self.assertEqual(run['program_review_history'][0]['state'], 'failed')
        self.assertNotIn('offline-only', run['program_review_history'][0]['error'])
        self.assertNotIn('https://example.invalid/v1', str(run['errors']))

    async def test_internal_repair_keeps_first_draft_and_every_actual_review_hash(self):
        calls = []
        async def two_reviews(template, run):
            calls.append(driver.messages_hash(run['messages']))
            return {'issues': [{'seq': 10, 'reason': 'offline finding'}] if len(calls) == 1 else [],
                    'goal_met': True, 'goal_reason': 'offline mocked response'}
        code, _ = await self.invoke(mode='repair_twice', review=two_reviews)
        out = self.root / 'outputs/独立随机-20261015/V01'
        run = json.loads((out / 'run.json').read_bytes())
        self.assertEqual(code, 0)
        self.assertEqual([entry['messages_sha256'] for entry in run['program_review_history']], calls)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(calls[-1], driver.messages_hash(run['messages']))
        snapshots = list((out / 'snapshots').iterdir())
        self.assertEqual(len(snapshots), 2)
        first = json.loads((sorted(snapshots)[0] / 'run.json').read_bytes())
        self.assertEqual(first['messages'][9]['content'], '离线测试台词10')

    async def test_final_hash_drift_after_review_is_rejected(self):
        code, _ = await self.invoke(mode='review_then_drift')
        self.assertEqual(code, 1)
        run = json.loads((self.root / 'outputs/独立随机-20261015/V01/run.json').read_bytes())
        self.assertEqual(run['program_review_history'][-1]['state'], 'returned')
        self.assertNotEqual(run['program_review_history'][-1]['messages_sha256'], driver.messages_hash(run['messages']))
        self.assertFalse(run['version_record']['counts_as_new_content'])

    async def test_setup_failure_keeps_new_attempt_without_starting_model_task(self):
        args = self.args()
        with patch.object(driver, 'OUT_ROOT', self.root / 'outputs/独立随机-20261015'), \
             patch.object(driver.base, 'configure_environment', side_effect=RuntimeError('offline setup failure')), \
             patch.object(api, 'start_simulation') as start:
            self.assertEqual(await driver.execute(args), 1)
            start.assert_not_called()
        out = self.root / 'outputs/独立随机-20261015/V01'
        self.assertTrue((out / 'template.json').is_file())
        self.assertFalse(json.loads((out / 'generation-report.json').read_bytes())['success'])

    def test_source_template_rejects_run_payload_disguised_as_template(self):
        template, _ = driver.read_template(driver.DEFAULT_TEMPLATE)
        path = self.root / 'template.json'
        path.write_text(json.dumps({**template, 'messages': [{'content': 'old'}]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '旧稿'):
            driver.read_template(path)
        with self.assertRaisesRegex(ValueError, '旧run'):
            driver.read_template(self.root / 'run.json')

    def test_archive_rejects_source_path_escape(self):
        for path in ('../outside.py', str((self.root / 'outside.py').resolve())):
            with self.subTest(path=path), patch.object(driver.base, 'provenance', return_value={
                    'commit': 'offline', 'source_hashes': {path: 'invalid'}}):
                with self.assertRaisesRegex(ValueError, '越出'):
                    driver.archive_sources(self.root / 'archive')

    def test_seeded_count_selection_feasibility_fallback_and_explicit_override(self):
        source = {'messages': []}
        fake = SimpleNamespace(min_total=lambda source, count: count * 14,
                               build_schedule=lambda source, count, target: [])
        selected = driver.choose_customer_count(fake, source, 'seed')
        self.assertEqual(selected, driver.choose_customer_count(fake, source, 'seed'))
        self.assertTrue(3 <= selected <= 10)
        self.assertGreater(len({driver.choose_customer_count(fake, source, str(i)) for i in range(20)}), 1)
        self.assertEqual(driver.choose_customer_count(fake, source, 'seed', 9), 9)
        with self.assertRaisesRegex(ValueError, '人数'):
            driver.choose_customer_count(fake, source, 'seed', 20)
        fake.min_total = lambda source, count: 100 if count == 1 else 151
        self.assertEqual(driver.choose_customer_count(fake, source, 'seed'), 1)
        fake.min_total = lambda source, count: 151
        with self.assertRaisesRegex(ValueError, '没有'):
            driver.choose_customer_count(fake, source, 'seed')


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        SANDBOX.cleanup()
