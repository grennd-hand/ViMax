import os
import unittest
from unittest.mock import AsyncMock, patch

from app import main


class ReviewBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_v4_honors_configured_budget_and_legacy_keeps_its_limit(self):
        template = {'source': {'messages': [], 'accounts': [], 'codes': []}, 'analysis': {}}
        for version, expected in ((3, 180), (4, 360)):
            run = {'config': {'dialogue_flow_version': version}, 'messages': []}
            model = AsyncMock(return_value={'issues': [], 'goal_met': True, 'goal_reason': 'test'})
            with patch.dict(os.environ, {'MODEL_CALL_BUDGET_SECONDS': '360'}), patch.object(main, 'model_json', model):
                await main.review_dialogue(template, run)
            self.assertEqual(model.await_count, 1)
            self.assertEqual(model.call_args.kwargs['call_budget_seconds'], expected)
