"""Offline patch-driver tests: temp SQLite, fake Chat, actual ViMax decode/archive."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tenacity import retry, stop_after_attempt

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = load('evolution_test_fixture', 'test-evolve-natural.py')
driver = load('evolution_patch_driver', 'evolve-natural-patch.py')
api, engine = fixture.api, fixture.engine


class PatchTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(fixture.EvolutionTests, 'previous'):
            fixture.EvolutionTests.setUpClass()
        cls.previous = fixture.EvolutionTests.previous
        cls.template = fixture.EvolutionTests.template
        cls.parent_bytes = fixture.EvolutionTests.parent_bytes

    async def child(self):
        return await driver.base.create_child(api, self.previous, self.template, 4,
                                              driver.base.sha(self.parent_bytes), {'reviews': {}}, {'commit': 'offline-test'})

    def writer(self, scenes, calls):
        class Writer:
            def __init__(self, chat_model):
                self.chat = chat_model
            async def write_script_based_on_story(self, story, request, **kwargs):
                calls.append(request)
                await self.chat.ainvoke([])
                return scenes
        return Writer

    async def apply(self, run, scenes, allowed, contract_errors=(), preserve_delay=False, reply_only_revision=False):
        calls = []
        with patch.object(engine, 'screenwriter_class', return_value=self.writer(scenes, calls)), \
             patch.object(engine, 'contract_errors', return_value=list(contract_errors)):
            result = await driver.patch_once(api, engine, self.template, run, self.previous['messages'],
                                            [{'seq': allowed[0], 'reason': 'offline actual test issue'}],
                                            fixture.FakeChat(), allowed, preserve_delay, reply_only_revision)
        return result, calls

    async def test_parent_delay_policy_preserves_raw_and_archives_actual_merged_delay(self):
        run = await self.child()
        scenes = ['032|客户7|0|0|自然群聊|文本|-|我想先看看电影版，再报片名。']
        result, calls = await self.apply(run, scenes, [32], preserve_delay=True)
        expected = self.previous['messages'][31]['delay']
        self.assertNotEqual(expected, '0')
        self.assertEqual(result['messages'][31]['delay'], expected)
        self.assertEqual(result['scoped_patch']['raw_patch_scenes'], scenes)
        self.assertEqual(result['scoped_patch']['retained_parent_delays'],
                         [{'seq': 32, 'field': 'delay', 'raw': '0', 'retained_parent': expected}])
        self.assertEqual(result['version_record']['delay_policy'], 'parent')
        self.assertIn('parent-delay', calls[0])
        archived = json.loads((api.DATA_DIR / result['candidate_versions'][0]['path'] / 'run.json').read_bytes())
        self.assertEqual(archived['messages'][31]['delay'], expected)

    def test_ranges_are_bounded_nonoverlapping_at_most_40(self):
        self.assertEqual(driver.parse_ranges('32-34,55-68'), list(range(32, 35)) + list(range(55, 69)))
        for value in ('', '0', '151', '34-32', '1-41', '1-3,3-4', '1-x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                driver.parse_ranges(value)

    def test_revision_cli_uses_separate_directory_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / 'outputs/自然群聊-20261015/V32/run.json'
            driver.base.atomic(original, b'original preserved')
            async def execute(args):
                self.assertEqual(args.revision_label, 'R001')
                driver.base.save_json(root / 'outputs/自然群聊-20261015/V32-R001/run.json',
                    {'status': 'paused', 'errors': [], 'messages': [{}]*150, 'program_review_deferred': True})
                return 1
            argv = ['evolve-natural-patch.py', '--version', '32', '--revision-label', 'R001',
                    '--parent', 'parent.json', '--issues', 'issues.json', '--editable-ranges', '35,44',
                    '--review-mode', 'deferred', '--branch-from-reviewed-best', '--preserve-delay']
            with patch.object(driver.base, 'ROOT', root), patch.object(driver.base, 'execute', execute), \
                 patch.object(driver, 'install_hooks') as hooks, patch('sys.argv', argv):
                self.assertEqual(driver.main(), 0)
                self.assertEqual(hooks.call_args.args[0].name, 'V32-R001')
                self.assertTrue(hooks.call_args.args[-2])
                self.assertFalse(hooks.call_args.args[-1])
            self.assertEqual(original.read_bytes(), b'original preserved')

    async def test_child_preserves_revision_parent_identity(self):
        previous = copy.deepcopy(self.previous)
        previous['version_record'].update(version='V28', revision='R001')
        child = await driver.base.create_child(api, previous, self.template, 32,
                    'exact-parent-hash', {'reviews': {}}, {'commit': 'offline-test'})
        self.assertEqual(child['version_record']['parent_version'], 'V28-R001')
        self.assertEqual(child['version_record']['version'], 'V32')

    async def test_outside_duplicate_reordering_empty_and_bad_media_are_attempts_only(self):
        rows = ['032|客户1|0|4|自然群聊|文本|-|我的新说法。',
                '033|客户2|0|4|自然群聊|文本|-|我知道了。']
        cases = [([rows[0]], [33], '超出'), ([rows[0], rows[0]], [32], '重复'),
                 ([rows[1], rows[0]], [32, 33], '递增'), ([], [32], '未返回'),
                 (['032|客户1|0|4|自然群聊|图片+文本|-|这里是图。'], [32], '图片码'),
                 (['not a line'], [32], '非编号')]
        for scenes, allowed, reason in cases:
            with self.subTest(reason=reason):
                run = await self.child()
                with self.assertRaisesRegex(ValueError, reason):
                    await self.apply(run, scenes, allowed)
                actual = api.get_run(run['id'])
                self.assertEqual(actual['messages'], [])
                self.assertFalse(actual.get('candidate_versions'))
                self.assertEqual(actual['scoped_patch']['raw_patch_scenes'], scenes)
                self.assertEqual(len(actual['vimax_samples']), 1)

    async def test_partial_patch_still_complete_150_and_preserves_all_outside_content_media(self):
        run = await self.child()
        scenes = ['032|客户1|0|4|自然群聊|文本|-|我家那个更爱看会说话的小动物。']
        result, calls = await self.apply(run, scenes, [32, 33, 34])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result['messages']), 150)
        self.assertEqual(result['cursor'], 150)
        self.assertEqual(result['version_record']['method'], 'scoped_patch')
        self.assertEqual(result['scoped_patch']['changed_sequences'], [32])
        for original, actual in zip(self.previous['messages'], result['messages']):
            if original['seq'] != 32:
                self.assertEqual(driver.base.dialogue_fingerprint({'messages': [original]}),
                                 driver.base.dialogue_fingerprint({'messages': [actual]}))
        self.assertIn('父版本完整150条公开对话', calls[0])
        self.assertIn(self.previous['messages'][-1]['content'], calls[0])
        self.assertEqual(len(result['candidate_versions']), 1)

    async def test_media_single_line_decode_and_contract_rejection_preserve_actual_merged_full_draft(self):
        run = await self.child()
        scenes = ['031|客户1|0|4|自然群聊|图片+文本|SCREENSHOT_OFFLINE_PATCH_31|我把这张发群里了。']
        with self.assertRaisesRegex(ValueError, 'injected structural failure'):
            await self.apply(run, scenes, [31], ['injected structural failure'])
        actual = api.get_run(run['id'])
        self.assertEqual(len(actual['messages']), 150)
        self.assertEqual(actual['messages'][30]['type'], '图片+文本')
        self.assertEqual(actual['messages'][30]['image_code'], 'SCREENSHOT_OFFLINE_PATCH_31')
        self.assertEqual(actual['messages'][30]['image_mode'], '素材占位')
        self.assertEqual(actual['scoped_patch']['changed_sequences'], [31])
        record = actual['candidate_versions'][0]
        captured = json.loads((api.DATA_DIR / record['path'] / 'run.json').read_bytes())
        self.assertEqual(len(captured['messages']), 150)
        self.assertEqual(captured['messages'][30]['image_code'], 'SCREENSHOT_OFFLINE_PATCH_31')

    async def test_no_content_change_is_not_a_new_version(self):
        original = self.previous['messages'][31]
        line = f"032|{original['sender']}|0|99|{original['stage']}|文本|-|{original['content']}"
        run = await self.child()
        with self.assertRaises(driver.base.NoChangeError):
            await self.apply(run, [line], [32])
        actual = api.get_run(run['id'])
        self.assertEqual(actual['messages'], [])
        self.assertTrue(actual['unchanged_parent_candidate'])
        self.assertEqual(len(actual['candidate_versions']), 1)

    async def reply_child(self):
        run = await self.child()
        run['version_record'].update(version='V40', revision='R005', parent_version='V40-R004')
        api.save_run(run)
        return run

    def reply_line(self, reply, content=None):
        original = self.previous['messages'][31]
        return f"032|{original['sender']}|{reply or 0}|0|{original['stage']}|文本|-|{original['content'] if content is None else content}"

    async def test_reply_revision_preserves_every_parent_field_and_raw_but_not_new_content(self):
        run = await self.reply_child()
        before = copy.deepcopy(self.previous['messages'])
        # Simulate backend-only metadata absent from model line format.
        self.previous['messages'][31]['offline_extra'] = {'immutable': [1, 2]}
        try:
            expected = copy.deepcopy(self.previous['messages'])
            target = 30 if expected[31].get('reply_to') != 30 else 29
            expected[31]['reply_to'] = target
            scenes = [self.reply_line(target)]
            result, calls = await self.apply(run, scenes, [32], preserve_delay=True, reply_only_revision=True)
            self.assertEqual(result['messages'], expected)
            self.assertEqual(result['scoped_patch']['raw_patch_scenes'], scenes)
            self.assertEqual(result['scoped_patch']['changed_sequences'], [32])
            self.assertEqual(result['scoped_patch']['content_changed_sequences'], [])
            self.assertFalse(result['version_record']['counts_as_new_content'])
            self.assertIn('显式reply-only修订', calls[0])
            self.assertNotIn('不能只调间隔、回复编号', calls[0])
            archived = json.loads((api.DATA_DIR / result['candidate_versions'][0]['path'] / 'run.json').read_bytes())
            self.assertEqual(archived['messages'], expected)
        finally:
            self.previous['messages'] = before

    async def test_reply_revision_rejects_noop_content_drift_and_invalid_target(self):
        original = self.previous['messages'][31]
        cases = [(self.reply_line(original.get('reply_to')), driver.base.NoChangeError, '未产生'),
                 (self.reply_line(30, '正文不能漂移。'), ValueError, 'content'),
                 (self.reply_line(32), ValueError, '未出现|此前消息')]
        for line, error, reason in cases:
            with self.subTest(reason=reason):
                run = await self.reply_child()
                with self.assertRaisesRegex(error, reason):
                    await self.apply(run, [line], [32], preserve_delay=True, reply_only_revision=True)
                self.assertEqual(api.get_run(run['id'])['messages'], [])

    async def test_reply_revision_rejects_cross_family_nonrevision_and_rollback_before_model(self):
        for changes in ({'parent_version': 'V39-R004'}, {'revision': None}, {'revision': 'R004'}, {'revision': 'R003'}):
            with self.subTest(changes=changes):
                run = await self.reply_child()
                run['version_record'].update(changes)
                with self.assertRaisesRegex(ValueError, '同family'):
                    await self.apply(run, [self.reply_line(30)], [32], preserve_delay=True, reply_only_revision=True)
                self.assertFalse(api.get_run(run['id']).get('vimax_samples'))

    def test_reply_revision_requires_full_and_delay_retention(self):
        for mode, delay in [('deferred', True), ('full', False)]:
            with self.assertRaisesRegex(ValueError, 'full'):
                driver.install_hooks(Path('V40-R005'), [90], mode, preserve_delay=delay, reply_only_revision=True)

    def test_reply_cli_requires_explicit_revision_before_execution(self):
        argv = ['evolve-natural-patch.py', '--version', '40', '--parent', 'parent.json',
                '--issues', 'issues.json', '--editable-ranges', '90', '--reply-only-revision', '--preserve-delay']
        with patch('sys.argv', argv), patch.object(driver.base, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'revision-label'):
                driver.main()
            execute.assert_not_called()

    async def test_deferred_never_calls_review_or_fabricates_pass(self):
        run = await self.child()
        run['messages'] = copy.deepcopy(self.previous['messages'])
        async def forbidden(*args, **kwargs):
            raise AssertionError('deferred called model review')
        with patch.object(api, 'review_dialogue', forbidden), patch.object(engine, 'contract_errors', return_value=[]):
            await driver.deferred_review(api, engine, self.template, run)
        self.assertEqual(run['status'], 'paused')
        self.assertEqual(run['review_progress']['phase'], 'awaiting_human_review')
        self.assertTrue(run['program_review_deferred'])
        self.assertNotIn('quality_review', run)
        self.assertNotIn('outcome_review', run)
        self.assertNotIn('program_review_history', run)

    async def test_deferred_resume_runs_real_full_review_and_snapshots_sources(self):
        run = await self.child()
        run['messages'] = copy.deepcopy(self.previous['messages'])
        run['program_review_deferred'] = True
        reviewed = []
        async def review(template, current):
            reviewed.append((len(current['messages']), api._run_reasoning_effort.get()))
            return {'issues': [{'seq': 32, 'reason': 'new real review finding'}],
                    'goal_met': True, 'goal_reason': 'actual completion test'}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(driver.base, 'provenance', wraps=driver.base.provenance), \
             patch.object(driver.base, 'revision_once', wraps=driver.base.revision_once), \
             patch.object(driver.base, 'review_only', wraps=driver.base.review_only), \
             patch.object(api, 'review_dialogue', review), patch.object(engine, 'contract_errors', return_value=[]):
            out = Path(folder)
            driver.install_hooks(out, [32], 'deferred', resume_review=True)
            provenance = driver.base.provenance()
            await driver.base.review_only(api, engine, self.template, run)
            self.assertEqual(provenance['review_mode'], 'full')
            for name in ('evolve-natural.py', 'evolve-natural-patch.py'):
                self.assertEqual((out / 'sources' / name).read_bytes(), (HERE / name).read_bytes())
                self.assertEqual(provenance['source_hashes']['.vimax/deploy/' + name], driver.base.sha((HERE / name).read_bytes()))
        self.assertEqual(reviewed, [(150, 'high')])
        self.assertFalse(run['program_review_deferred'])
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['version_record']['review_mode'], 'full')
        self.assertEqual(len(run['program_review_history']), 1)

    async def test_upstream_three_attempt_wrapper_is_bypassed(self):
        run = await self.child()
        calls = []
        class Writer:
            def __init__(self, chat_model):
                pass
            @retry(stop=stop_after_attempt(3))
            async def write_script_based_on_story(self, *args, **kwargs):
                calls.append(True)
                raise RuntimeError('offline injected network failure')
        with patch.object(engine, 'screenwriter_class', return_value=Writer):
            with self.assertRaisesRegex(RuntimeError, 'offline injected network failure'):
                await driver.patch_once(api, engine, self.template, run, self.previous['messages'],
                                        [], fixture.FakeChat(), [32])
        self.assertEqual(len(calls), 1)
        self.assertEqual(api.get_run(run['id'])['messages'], [])

    async def test_deferred_still_runs_full_contract_and_fails_honestly(self):
        run = await self.child()
        run['messages'] = copy.deepcopy(self.previous['messages'])
        with patch.object(engine, 'contract_errors', return_value=['真实全稿合同错误']) as contracts:
            await driver.deferred_review(api, engine, self.template, run)
        self.assertEqual(len(contracts.call_args.args[1]['messages']), 150)
        self.assertEqual(contracts.call_args.args[1]['status'], 'completed')
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['errors'], ['真实全稿合同错误'])
        self.assertNotIn('outcome_review', run)

    async def test_real_v05_schedule_mismatch_is_aligned_before_real_contract(self):
        path = driver.base.ROOT / 'outputs/自然群聊-20261015/V05/run.json'
        original_bytes = path.read_bytes()
        previous = json.loads(original_bytes)
        self.assertEqual(len(previous['messages']), 150)
        baseline = engine.contract_errors(self.template['source'], {**previous, 'status': 'completed'})
        self.assertEqual(baseline, ['对话阶段顺序与计划不符'])
        run = await driver.base.create_child(api, previous, self.template, 6,
                                              driver.base.sha(original_bytes), {'reviews': {}}, {'commit': 'offline-regression'})
        self.assertNotEqual([slot['stage'] for slot in run['schedule']], [m['stage'] for m in previous['messages']])
        message = previous['messages'][62]
        line = f"063|{message['sender']}|0|4|{message['stage']}|文本|-|这段我回头找来看看。"
        with patch.object(engine, 'screenwriter_class', return_value=self.writer([line], [])), \
             patch.object(engine, 'contract_errors', wraps=engine.contract_errors) as contracts, \
             patch.object(driver.base, 'broadcast'):
            result = await driver.patch_once(api, engine, self.template, run, previous['messages'],
                                            [], fixture.FakeChat(), [63])
        self.assertEqual(contracts.call_count, 1)
        self.assertEqual(engine.contract_errors(self.template['source'], {**result, 'status': 'completed'}), [])
        self.assertEqual([slot['stage'] for slot in result['schedule']], [m['stage'] for m in result['messages']])
        captured = json.loads((api.DATA_DIR / result['candidate_versions'][0]['path'] / 'run.json').read_bytes())
        self.assertEqual([slot['stage'] for slot in captured['schedule']], [m['stage'] for m in captured['messages']])
        self.assertEqual(path.read_bytes(), original_bytes)

    async def test_schedule_alignment_keeps_real_opening_and_monotonic_stage_validation(self):
        run = await self.child()
        scenes = ['001|坐席|0|4|自然群聊|文本|-|大家好，刚坐下来。']
        with patch.object(engine, 'screenwriter_class', return_value=self.writer(scenes, [])):
            with self.assertRaisesRegex(ValueError, '开场顺序不连贯|第一条应先打招呼暖场'):
                await driver.patch_once(api, engine, self.template, run, self.previous['messages'],
                                        [], fixture.FakeChat(), [1])
        actual = api.get_run(run['id'])
        self.assertEqual(len(actual['messages']), 150)
        errors = engine.contract_errors(self.template['source'], {**actual, 'status': 'completed'})
        self.assertNotIn('对话阶段顺序与计划不符', errors)
        self.assertTrue(any('开场顺序不连贯' in error for error in errors))
        self.assertTrue(any('第一条应先打招呼暖场' in error for error in errors))

    async def test_branch_hooks_record_real_parent_and_target_without_changing_parent(self):
        before = copy.deepcopy(self.previous)
        run = await self.child()
        self.assertEqual(run['version_record']['parent_version'], 'V03')
        self.assertEqual(run['version_record']['parent_run'], before['id'])
        self.assertEqual(run['config']['persona_cards'], before['config']['persona_cards'])
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(driver.base, 'provenance', wraps=driver.base.provenance), \
             patch.object(driver.base, 'revision_once', wraps=driver.base.revision_once), \
             patch.object(driver.base, 'review_only', wraps=driver.base.review_only), \
             patch.object(driver.base, 'read_inputs', wraps=driver.base.read_inputs) as read_inputs, \
             patch.object(driver, 'patch_once', return_value=run) as patch_once:
            out = Path(folder) / 'V07'
            driver.install_hooks(out, [32], 'deferred', branch_from_reviewed_best=True)
            await driver.base.revision_once(api, engine, self.template, run, before['messages'], [], fixture.FakeChat())
            provenance = driver.base.provenance()
            self.assertEqual(provenance['branch_mode'], 'reviewed_best')
            self.assertEqual(provenance['target_version'], 'V07')
            self.assertEqual(run['version_record']['parent_version'], 'V03')
            self.assertEqual(run['version_record']['branch_mode'], 'reviewed_best')
            self.assertEqual(run['version_record']['target_version'], 'V07')
            read_inputs.return_value = (before, self.template, {}, 'test-hash')
            driver.base.read_inputs(Path('parent'), Path('issues'), 7)
            self.assertTrue(read_inputs.call_args.kwargs['allow_reviewed_best'])
            patch_once.assert_awaited_once()
        self.assertEqual(self.previous, before)


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(fixture.EvolutionTests, 'previous'):
            fixture.EvolutionTests.setUpClass()

    def test_default_rejects_skipped_parent_and_explicit_branch_accepts_earlier_with_dual_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'V03'
            parent = copy.deepcopy(fixture.EvolutionTests.previous)
            template = fixture.EvolutionTests.template
            driver.base.save_json(root / 'template.json', template)
            def write_parent_and_reviews():
                driver.base.save_json(root / 'run.json', parent)
                parent_bytes = (root / 'run.json').read_bytes()
                evidence = {'issues': [], 'reviews': {role: {'full_read': True, 'message_count': 150,
                           'run_sha256': driver.base.sha(parent_bytes), 'summary': 'offline real hash full-read fixture'}
                           for role in ('main', 'subagent')}}
                driver.base.save_json(root / 'issues.json', evidence)
                return parent_bytes
            original = write_parent_and_reviews()
            with self.assertRaisesRegex(ValueError, '顺序迭代'):
                driver.base.read_inputs(root / 'run.json', root / 'issues.json', 7)
            actual, _, _, digest = driver.base.read_inputs(root / 'run.json', root / 'issues.json', 7, allow_reviewed_best=True)
            self.assertEqual(actual['version_record']['version'], 'V03')
            self.assertEqual(digest, driver.base.sha(original))
            self.assertEqual((root / 'run.json').read_bytes(), original)
            evidence = json.loads((root / 'issues.json').read_bytes())
            evidence['reviews']['subagent']['run_sha256'] = 'wrong'
            driver.base.save_json(root / 'issues.json', evidence)
            with self.assertRaisesRegex(ValueError, 'subagent'):
                driver.base.read_inputs(root / 'run.json', root / 'issues.json', 7, allow_reviewed_best=True)
            for parent_version in ('V07', 'V08', 'V00', 'not-a-version'):
                parent['version_record']['version'] = parent_version
                write_parent_and_reviews()
                with self.subTest(parent_version=parent_version), self.assertRaisesRegex(ValueError, '严格早于'):
                    driver.base.read_inputs(root / 'run.json', root / 'issues.json', 7, allow_reviewed_best=True)
            parent['version_record']['version'] = 'V03'
            parent['template_id'] = 'different-mother'
            write_parent_and_reviews()
            with self.assertRaisesRegex(ValueError, '母本不匹配'):
                driver.base.read_inputs(root / 'run.json', root / 'issues.json', 7, allow_reviewed_best=True)

    def test_branch_locks_coexist_for_different_versions_and_reject_same_version(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(driver.base, 'ROOT', Path(folder)):
            first = driver.execution_lock_path(7, True)
            second = driver.execution_lock_path(8, True)
            self.assertNotEqual(first, second)
            self.assertEqual(driver.execution_lock_path(7), driver.execution_lock_path(8))
            with driver.base.exclusive_lock(first), driver.base.exclusive_lock(second):
                with self.assertRaisesRegex(RuntimeError, '正在运行'):
                    with driver.base.exclusive_lock(driver.execution_lock_path(7, True)):
                        self.fail('same-version concurrent lock was accepted')

    def test_only_valid_deferred_paused_draft_maps_exit_one_to_zero(self):
        cases = [({'status': 'paused', 'errors': [], 'messages': [{}] * 150,
                   'program_review_deferred': True}, 0),
                 ({'status': 'failed', 'errors': ['bad'], 'messages': [{}] * 150,
                   'program_review_deferred': True}, 1),
                 ({'status': 'paused', 'errors': [], 'messages': [],
                   'program_review_deferred': True}, 1),
                 ({'status': 'paused', 'errors': [], 'messages': [{}] * 150,
                   'program_review_deferred': False}, 1)]
        for run, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                async def execute(args):
                    driver.base.save_json(root / 'outputs/自然群聊-20261015/V05/run.json', run)
                    return 1
                argv = ['evolve-natural-patch.py', '--version', '05', '--parent', 'parent.json',
                        '--issues', 'issues.json', '--editable-ranges', '32-34', '--review-mode', 'deferred']
                with patch.object(driver.base, 'ROOT', root), patch.object(driver.base, 'execute', execute), \
                     patch.object(driver, 'install_hooks'), patch('sys.argv', argv):
                    result = driver.main()
                self.assertEqual(result, expected)
                self.assertEqual(json.loads((root / 'outputs/自然群聊-20261015/V05/run.json').read_bytes()), run)


class RevisionParentTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(fixture.EvolutionTests, 'previous'):
            fixture.EvolutionTests.setUpClass()

    def case(self, root, family=40, revision=None, dirname=None):
        run = copy.deepcopy(fixture.EvolutionTests.previous)
        run['version_record'] = {'version': f'V{family:02d}'}
        if revision is not None:
            run['version_record']['revision'] = revision
        label = dirname or f'V{family:02d}' + ('-' + revision if revision is not None else '')
        folder = root / 'outputs/自然群聊-20261015' / label
        driver.base.save_json(folder / 'run.json', run)
        driver.base.save_json(folder / 'template.json', fixture.EvolutionTests.template)
        payload = (folder / 'run.json').read_bytes()
        fields = ('seq', 'sender', 'content', 'reply_to', 'delay', 'stage', 'type',
                  'image_code', 'image_mode', 'replace_date')
        digest = driver.base.sha(json.dumps([{k: m.get(k) for k in fields} for m in run['messages']],
                                ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        evidence = {'issues': [], 'reviews': {}}
        for role, name in (('main', 'R000-primary-review.md'), ('subagent', 'R000-subagent-review.md')):
            report = folder / 'reviews' / name
            driver.base.atomic(report, ('Offline test report; actual message hash: ' + digest).encode())
            evidence['reviews'][role] = {'full_read': True, 'message_count': 150,
                'run_sha256': driver.base.sha(payload), 'summary': 'offline full-read test fixture',
                'report': f'{label}/reviews/{name}'}
        issues = root / 'issues.json'
        driver.base.save_json(issues, evidence)
        return folder / 'run.json', issues, evidence

    async def test_same_family_requires_explicit_strictly_newer_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            parent, issues, _ = self.case(root)
            for branch in (False, True):
                with self.subTest(branch=branch), self.assertRaises(ValueError):
                    driver.base.read_inputs(parent, issues, 40, allow_reviewed_best=branch)
            accepted = driver.base.read_inputs(parent, issues, 40, revision_label='R001')
            self.assertEqual(accepted[0]['version_record'], {'version': 'V40'})
            with self.assertRaisesRegex(ValueError, '严格大于'):
                driver.base.read_inputs(parent, issues, 40, revision_label='R000')
            parent, issues, _ = self.case(root, revision='R001')
            driver.base.read_inputs(parent, issues, 40, revision_label='R002')
            for label in ('R000', 'R001'):
                with self.subTest(label=label), self.assertRaisesRegex(ValueError, '严格大于'):
                    driver.base.read_inputs(parent, issues, 40, revision_label=label)
            for label in ('R-01', 'R1', 'R1000', '../R001', 'R００１'):
                with self.subTest(label=label), self.assertRaisesRegex(ValueError, '修订标记'):
                    driver.base.read_inputs(parent, issues, 40, revision_label=label)

    async def test_future_family_and_directory_embedded_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            parent, issues, _ = self.case(root)
            with self.assertRaisesRegex(ValueError, '严格早于'):
                driver.base.read_inputs(parent, issues, 39, allow_reviewed_best=True, revision_label='R001')
            for revision, dirname in ((None, 'V39'), ('R001', 'V40-R002'), (None, 'V40-R000')):
                parent, issues, _ = self.case(root, revision=revision, dirname=dirname)
                with self.subTest(dirname=dirname), self.assertRaisesRegex(ValueError, '目录与embedded'):
                    driver.base.read_inputs(parent, issues, 40, revision_label='R003')

    async def test_revision_still_requires_exact_run_hash_and_both_real_bound_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for role in ('main', 'subagent'):
                parent, issues, evidence = self.case(root)
                evidence['reviews'][role]['run_sha256'] = 'stale'
                driver.base.save_json(issues, evidence)
                with self.subTest(role=role), self.assertRaisesRegex(ValueError, role):
                    driver.base.read_inputs(parent, issues, 40, revision_label='R001')
                for failure in ('missing', 'stale', 'escape'):
                    parent, issues, evidence = self.case(root)
                    report = parent.parent.parent / evidence['reviews'][role]['report']
                    if failure == 'missing':
                        report.unlink()
                    elif failure == 'stale':
                        report.write_text('old message hash', encoding='utf-8')
                    else:
                        outside = parent.parent.parent / 'outside-review.md'
                        outside.write_bytes(report.read_bytes())
                        evidence['reviews'][role]['report'] = str(outside)
                        driver.base.save_json(issues, evidence)
                    with self.subTest(role=role, failure=failure), self.assertRaisesRegex(ValueError, role):
                        driver.base.read_inputs(parent, issues, 40, revision_label='R001')

    async def test_actual_execute_creates_new_revision_without_overwriting_parent_or_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            parent, issues, _ = self.case(root)
            original = parent.read_bytes()
            args = SimpleNamespace(version=40, parent=parent, issues=issues,
                                   resume_review=False, revision_label='R001')
            async def revise(current_api, current_engine, template, run, messages, evidence, chat):
                run['messages'] = copy.deepcopy(messages)
                run['messages'][76]['content'] = '我看得见那团东西，手指夹不住。'
                run['cursor'] = 150
                current_api.save_run(run)
                return run
            async def review(current_api, current_engine, template, run):
                run['status'] = 'completed'
                current_api.save_run(run)
            with patch.object(driver.base, 'ROOT', root), \
                 patch.object(driver.base, 'configure_environment'), \
                 patch.object(api, 'put_template'), \
                 patch.object(driver.base, 'provenance', return_value={'commit': 'offline-test'}), \
                 patch.object(driver.base, 'revision_once', revise), \
                 patch.object(driver.base, 'review_only', review):
                self.assertEqual(await driver.base.execute(args), 0)
            child = json.loads((root / 'outputs/自然群聊-20261015/V40-R001/run.json').read_bytes())
            self.assertEqual(child['version_record']['version'], 'V40')
            self.assertEqual(child['version_record']['revision'], 'R001')
            self.assertEqual(child['version_record']['parent_version'], 'V40')
            self.assertEqual(child['version_record']['parent_sha256'], driver.base.sha(original))
            self.assertEqual(len(child['messages']), 150)
            self.assertEqual(parent.read_bytes(), original)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        fixture.SANDBOX.cleanup()
