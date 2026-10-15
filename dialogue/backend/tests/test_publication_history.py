import asyncio
import copy
import atexit
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
_data = tempfile.TemporaryDirectory(prefix='publication-api-test-')
atexit.register(_data.cleanup)
os.environ.setdefault('SCRIPT_GENERATOR_DATA_DIR', _data.name)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main as api


class PublicationHistory(unittest.TestCase):
    def test_archived_resume_rejected_without_writes_or_tasks(self):
        for status in ('paused', 'failed'):
            run = {'id': 'archived', 'status': status, 'messages': [], 'publication': {'read_only': True}}
            before = copy.deepcopy(run)
            with patch.object(api, 'get_run', return_value=run), patch.object(api, 'save_run') as save:
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(api.resume('archived'))
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn('已归档版本', error.exception.detail)
            self.assertEqual(run, before)
            save.assert_not_called()

    def test_workspace_contains_original_version_and_honest_review_metadata(self):
        run = {'id': 'archived', 'template_id': 'mother', 'status': 'paused', 'messages': [],
               'config': {'target_messages': 150}, 'publication': {'read_only': True, 'human_verdict': 'alternative'},
               'version_record': {'version': 'V06'}, 'program_review_deferred': True}
        template = {'id': 'mother', 'source': {'messages': [], 'accounts': []}, 'analysis': {}}
        with patch.object(api, 'get_run', return_value=run), patch.object(api, 'get_template', return_value=template), \
             patch.object(api, 'min_total', return_value=0):
            result = api.simulation_detail('archived', view='workspace')
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['publication'], run['publication'])
        self.assertEqual(result['version_record'], {'version': 'V06'})
        self.assertTrue(result['program_review_deferred'])
        self.assertNotIn('outcome_review', result)


if __name__ == '__main__':
    unittest.main()
