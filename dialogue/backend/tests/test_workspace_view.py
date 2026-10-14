import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import main


class WorkspaceViewTests(unittest.TestCase):
    def test_compact_view_keeps_readable_history_and_template_without_internal_drafts(self):
        run = {'id':'history','template_id':'mother','status':'completed','messages':[{'seq':1,'sender':'坐席','content':'你好'}],
               'config':{'target_messages':150},'agent_sessions':{'坐席':{'turns':1,'memory_events':[]}},
               'schedule':[{'kind':'seat'}]*150,'vimax_draft':[{'content':'previous draft'}],'vimax_raw':'internal draft',
               'cursor':150,'memory_reflections':[]}
        template = {'id':'mother','source':{'messages':[]},'analysis':{'primary_goal':'目标'}}
        original = copy.deepcopy(run)
        with patch.object(main,'get_run',return_value=run),patch.object(main,'get_template',return_value=template), \
             patch.object(main,'validate_run',return_value=[]),patch.object(main,'decision_ledger',return_value=[]), \
             patch.object(main,'scene_ledger',return_value=[]):
            full = main.simulation_detail('history')
            compact = main.simulation_detail('history',view='workspace')
        self.assertEqual(compact['messages'],full['messages'])
        self.assertEqual(compact['config'],full['config'])
        self.assertEqual(compact['agent_sessions'],full['agent_sessions'])
        self.assertEqual(compact['template']['id'],'mother')
        self.assertEqual(compact['schedule_length'],150)
        self.assertNotIn('vimax_draft',compact)
        self.assertNotIn('vimax_raw',compact)
        self.assertNotIn('schedule',compact)
        self.assertIn('vimax_draft',full)
        self.assertEqual(run,original)

    def test_paused_view_does_not_run_completed_result_validation(self):
        run={'id':'paused','template_id':'mother','status':'paused','messages':[],'config':{},'schedule':[{}]*150}
        template={'id':'mother','source':{'messages':[]},'analysis':{}}
        with patch.object(main,'get_run',return_value=run),patch.object(main,'get_template',return_value=template), \
             patch.object(main,'validate_run') as validate,patch.object(main,'decision_ledger',return_value=[]), \
             patch.object(main,'scene_ledger',return_value=[]):
            compact=main.simulation_detail('paused',view='workspace')
        validate.assert_not_called()
        self.assertEqual(compact['status'],'paused')
        self.assertEqual(compact['validation'],[])
