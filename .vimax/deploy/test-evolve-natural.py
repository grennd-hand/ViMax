"""Isolated tests only: temporary SQLite, fake model, no network/real output writes."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('evolution_driver', HERE / 'evolve-natural.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
SANDBOX = tempfile.TemporaryDirectory(prefix='vimax-evolve-test-')
os.environ.update(SCRIPT_GENERATOR_DATA_DIR=str(Path(SANDBOX.name) / 'runtime'),
                  MODEL_API_BASE_URL='https://example.invalid/v1', MODEL_API_KEY='offline-test-only',
                  MODEL_NAME='gpt-6-sol', MODEL_DIALOGUE_NAME='gpt-6-sol', MODEL_REVIEW_NAME='gpt-6-sol')
sys.path[:0] = [str(driver.ROOT), str(driver.ROOT / 'dialogue/backend')]
from app import main as api
from app import vimax_engine as engine
from app.core import export_xlsx


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeChat:
    model_name = 'gpt-6-sol'
    reasoning_effort = 'high'

    async def ainvoke(self, _):
        return FakeResponse('raw complete model response retained')


class EvolutionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Read-only fixture: the template and parent remain byte-for-byte unchanged.
        cls.parent_path = driver.ROOT / 'outputs/自然群聊-20261015/V03/run.json'
        cls.parent_bytes = cls.parent_path.read_bytes()
        cls.previous = json.loads(cls.parent_bytes)
        cls.template = json.loads((cls.parent_path.parent / 'template.json').read_bytes())
        api.put_template(cls.template['id'], cls.template['filename'], cls.template['source'], cls.template['analysis'])

    async def child(self):
        return await driver.create_child(api, self.previous, self.template, 4,
                                         driver.sha(self.parent_bytes), {'reviews': {'main': {}, 'subagent': {}}},
                                         {'commit': 'offline-test', 'model': driver.MODEL})

    async def test_fresh_id_persona_world_no_auto_plan_or_inherited_memories(self):
        started = []
        async def forbidden_plan(_):
            started.append(True)
            raise AssertionError('automatic planning ran')
        with patch.object(api, 'process_run', forbidden_plan):
            run = await self.child()
        self.assertFalse(started)
        self.assertNotEqual(run['id'], self.previous['id'])
        self.assertNotEqual(run['config']['story_world'], self.previous['config']['story_world'])
        self.assertEqual(run['config']['persona_cards'], self.previous['config']['persona_cards'])
        self.assertNotIn('vimax_project_id', run)
        self.assertEqual(run['messages'], [])
        for key in ('candidate_versions', 'vimax_calls', 'vimax_samples', 'memory_reflections'):
            self.assertNotIn(key, run)
        for state in run['agent_sessions'].values():
            self.assertEqual(state['memory'], '')
            self.assertEqual(state['turns'], 0)
        self.assertEqual(run['person_situations'], self.previous['person_situations'])
        self.assertEqual(run['conversation_threads'], self.previous['conversation_threads'])
        self.assertEqual(self.parent_path.read_bytes(), self.parent_bytes)

    async def test_complete_structural_rejection_keeps_actual_candidate_and_raw_once(self):
        run = await self.child()
        messages = copy.deepcopy(self.previous['messages'])
        messages[0]['content'] += ' 新候选隔离测试'
        calls = []
        class Writer:
            def __init__(self, chat_model):
                self.chat = chat_model
            async def write_script_based_on_story(self, *args, **kwargs):
                calls.append(True)
                await self.chat.ainvoke([])
                return ['fake parsed scenes']
        with patch.object(engine, 'screenwriter_class', return_value=Writer), \
             patch.object(engine, 'decode_scenes', return_value=messages), \
             patch.object(engine, 'contract_errors', return_value=['injected full-candidate rejection']):
            with self.assertRaisesRegex(ValueError, 'injected'):
                await driver.revision_once(api, engine, self.template, run, self.previous['messages'], [], FakeChat())
        actual = api.get_run(run['id'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(actual['messages'], messages)
        self.assertEqual(len(actual['candidate_versions']), 1)
        self.assertEqual(actual['vimax_samples'][0]['content'], 'raw complete model response retained')
        self.assertEqual([m.get('image_code') for m in actual['messages']], [m.get('image_code') for m in self.previous['messages']])
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            driver.capture_artifacts(api, out, actual, export_xlsx)
            self.assertTrue((out / '群聊150条.xlsx').exists())
            self.assertTrue((out / '群聊150条.md').exists())
            self.assertEqual(len(list((out / 'snapshots').glob('*/manifest.json'))), 1)
            self.assertEqual(json.loads((out / 'run.json').read_bytes())['messages'], messages)

    async def test_network_failure_never_exports_parent_as_new_version(self):
        run = await self.child()
        async def failing(*args, **kwargs):
            raise RuntimeError('offline injected disconnect')
        with patch.object(engine, 'revise', failing), patch.object(engine, 'screenwriter_class', return_value=object):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                await driver.revision_once(api, engine, self.template, run, self.previous['messages'], [], FakeChat())
        self.assertEqual(api.get_run(run['id'])['messages'], [])

    async def test_same_wording_is_attempt_only(self):
        run = await self.child()
        async def unchanged(writer, chat, current_api, identifier, template, editing, issues, reason):
            engine.checkpoint_candidate(current_api, editing, editing['messages'], 'offline unchanged candidate')
            return []
        with patch.object(engine, 'revise', unchanged), patch.object(engine, 'screenwriter_class', return_value=object):
            with self.assertRaises(driver.NoChangeError):
                await driver.revision_once(api, engine, self.template, run, self.previous['messages'], [], FakeChat())
        actual = api.get_run(run['id'])
        self.assertEqual(actual['messages'], [])
        self.assertTrue(actual['unchanged_parent_candidate'])
        self.assertEqual(len(actual['candidate_versions']), 1)

    async def test_review_only_forces_high_keeps_script_and_does_not_revise(self):
        run = await self.child()
        run['messages'] = copy.deepcopy(self.previous['messages'])
        run['cursor'] = 150
        calls = []
        async def review(template, current):
            calls.append(api._run_reasoning_effort.get())
            return {'issues': [{'seq': 2, 'reason': 'test issue for next version'}],
                    'goal_met': True, 'goal_reason': 'test actual completion'}
        async def forbidden(*args, **kwargs):
            raise AssertionError('review must not generate another draft')
        before = driver.dialogue_fingerprint(run)
        with patch.object(api, 'review_dialogue', review), patch.object(engine, 'revise', forbidden), \
             patch.object(engine, 'contract_errors', return_value=[]):
            await driver.review_only(api, engine, self.template, run)
        self.assertEqual(calls, ['high'])
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['quality_review'][0]['reason'], 'test issue for next version')
        self.assertEqual(before, driver.dialogue_fingerprint(run))
        self.assertEqual(len(run['program_review_history']), 1)

    async def test_real_framework_broadcast_rebuilds_all_150_without_model_calls(self):
        run = await self.child()
        run['messages'] = copy.deepcopy(self.previous['messages'])
        expected = driver.dialogue_fingerprint(run)
        await driver.broadcast(api, engine, self.template, run)
        self.assertEqual(len(run['messages']), 150)
        self.assertEqual(run['cursor'], 150)
        self.assertEqual(expected, driver.dialogue_fingerprint(run))
        self.assertEqual(sum(state['turns'] for state in run['agent_sessions'].values()), 150)
        self.assertNotIn('vimax_calls', run)

    async def test_delay_or_reply_only_does_not_count_as_content_iteration(self):
        run = {'messages': copy.deepcopy(self.previous['messages'])}
        changed = copy.deepcopy(run)
        changed['messages'][0]['delay'] = 99
        changed['messages'][1]['reply_to'] = 0
        self.assertNotEqual(driver.dialogue_fingerprint(changed), driver.dialogue_fingerprint(run))
        self.assertEqual(driver.content_fingerprint(changed), driver.content_fingerprint(run))

    async def test_inputs_require_both_readers_and_exact_parent_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'V03'
            out.mkdir()
            (out / 'run.json').write_bytes(self.parent_bytes)
            driver.save_json(out / 'template.json', self.template)
            evidence = {'issues': [], 'reviews': {role: {'full_read': True, 'message_count': 150,
                       'run_sha256': driver.sha(self.parent_bytes), 'summary': 'full read, actual findings retained'}
                       for role in ('main', 'subagent')}}
            driver.save_json(out / 'issues.json', evidence)
            driver.read_inputs(out / 'run.json', out / 'issues.json', 4)
            evidence['reviews']['subagent']['run_sha256'] = 'wrong'
            driver.save_json(out / 'issues.json', evidence)
            with self.assertRaisesRegex(ValueError, 'subagent'):
                driver.read_inputs(out / 'run.json', out / 'issues.json', 4)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        SANDBOX.cleanup()
