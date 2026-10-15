import asyncio
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import main as api
from app.dialogue_corpus import parse_lccc, save_corpus


class IndependentStartTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, **overrides):
        saved = []
        template = {'id': 'mother', 'source': {'accounts': [], 'messages': []},
                    'analysis': {'confirmed': True, 'primary_goal': 'fixture'}}
        def create(run):
            saved.append(copy.deepcopy(run))
        def defer(coroutine):
            coroutine.close()
            return object()
        with tempfile.TemporaryDirectory() as directory:
            corpus_path = Path(directory) / 'corpus.json'
            save_corpus(parse_lccc([['工作 结束 了', '刚刚 到家'], ['这个 游戏 真难', '我试试']],
                                  {'name': 'fixture', 'url': 'https://example.invalid/data', 'license': 'fixture'}), corpus_path)
            config = api.SimulationConfig(template_id='mother', customer_count=3, target_messages=150,
                                          dialogue_model='gpt-6-sol', reasoning_effort='high', **overrides)
            with patch.object(api, 'get_template', return_value=template), \
                 patch.object(api, 'model_settings', return_value=('https://example.invalid', 'fixture', 'gpt-6-sol')), \
                 patch.object(api, 'build_schedule', return_value=[{'stage': '自然群聊'} for _ in range(150)]), \
                 patch.object(api, 'goal_requires_action', return_value=True), \
                 patch.object(api, 'CORPUS_PATH', corpus_path), patch.object(api, 'create_run', side_effect=create), \
                 patch.object(api.asyncio, 'create_task', side_effect=defer):
                first = await api.start_simulation(config)
                second = await api.start_simulation(config)
            for result in (first, second):
                api.tasks.pop(result['id'])
        return saved

    async def test_same_explicit_selection_seed_still_creates_empty_isolated_runs(self):
        runs = await self.start(variation_seed='repeatable-plan', story_world='old-world', character_ids={'客户1': 'old-character'})
        self.assertNotEqual(runs[0]['id'], runs[1]['id'])
        self.assertNotEqual(runs[0]['config']['story_world'], runs[1]['config']['story_world'])
        self.assertEqual(runs[0]['creative_variation'], runs[1]['creative_variation'])
        for run in runs:
            self.assertEqual(run['messages'], [])
            self.assertEqual(run['config']['usage_context'], 'fictional_rehearsal')
            self.assertEqual(run['config']['character_ids'], {})
            self.assertNotIn('vimax_story', run)
            self.assertNotIn('person_situations', run)
            self.assertIsNone(run['independence']['parent_run'])
            self.assertEqual(run['independence']['initial_messages'], 0)
            self.assertEqual(len(run['corpus_reference']['samples']), 2)
            self.assertTrue(all(not state['memory_events'] for state in run['agent_sessions'].values()))

    async def test_default_new_runs_get_different_seeds_and_plans(self):
        runs = await self.start()
        self.assertNotEqual(runs[0]['creative_variation']['seed'], runs[1]['creative_variation']['seed'])
        self.assertNotEqual(runs[0]['creative_variation'], runs[1]['creative_variation'])

    async def test_current_persona_choice_preserves_explicit_identity_with_fresh_world(self):
        runs = await self.start(randomize_personas=False, personas={'客户1': '话少但很爱音乐'}, story_world='old')
        self.assertEqual(runs[0]['config']['persona_cards']['客户1']['identity'], '话少但很爱音乐')
        self.assertEqual(runs[0]['creative_variation']['fixed_roles'], ['客户1'])
        self.assertNotEqual(runs[0]['config']['story_world'], 'old')

    async def test_fixed_mode_does_not_replace_explicit_persona_or_attach_random_profile(self):
        runs = await self.start(creation_mode='fixed', persona_cards={'客户1': {'identity': '用户选择的人物'}}, story_world='keep-world')
        for run in runs:
            self.assertNotIn('creative_variation', run)
            self.assertEqual(run['config']['persona_cards']['客户1']['identity'], '用户选择的人物')
            self.assertEqual(run['config']['story_world'], 'keep-world')

    async def test_retry_of_independent_run_draws_new_seed_without_changing_old_config(self):
        config = {'template_id': 'mother', 'customer_count': 3, 'target_messages': 150,
                  'creation_mode': 'independent_random', 'variation_seed': 'old-seed'}
        original = copy.deepcopy(config)
        with patch.object(api, 'get_run', return_value={'config': config}), \
             patch.object(api, 'start_simulation', new_callable=AsyncMock, return_value={'id': 'new'}) as start:
            self.assertEqual(await api.retry('old'), {'id': 'new'})
        self.assertIsNone(start.call_args.args[0].variation_seed)
        self.assertEqual(config, original)

    def test_independent_mode_does_not_query_previous_story_cache(self):
        with patch.object(api, 'db') as database:
            self.assertIsNone(api.cached_story_arc({'config': {'creation_mode': 'independent_random'}}))
        database.assert_not_called()


if __name__ == '__main__':
    unittest.main()
