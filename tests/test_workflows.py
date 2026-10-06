import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import create_app
import app as app_module
from state_store import StateStore
from poster_service import PosterService
from jobs import JobQueue


ITEM = {'id': 'show', 'title': 'Example', 'type': 'Series', 'year': 2020}
SELECTION = {
    'series_poster_url': 'https://theposterdb.com/api/assets/1',
    'season_posters': {'season': {'url': 'https://theposterdb.com/api/assets/2', 'title': 'Season 1'}},
}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network is disabled in tests'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.store = StateStore(os.path.join(self.temp.name, 'state.sqlite3'))
        self.service = PosterService(self.store)
        self.queue = JobQueue(self.store, self.service, delay=0, start_worker=False)
        self.app = create_app({
            'TESTING': True, 'SECRET_KEY': 'test', 'APP_STATE_DIR': self.temp.name,
            'CACHE_DIR': self.temp.name,
        }, store=self.store, job_queue=self.queue)
        self.client = self.app.test_client()


class WorkflowTests(OfflineCase):
    def test_liveness_does_not_call_jellyfin(self):
        with patch('app.scraper.get_jellyfin_server_info') as server_info:
            response = self.client.get('/health/live')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'alive'})
        server_info.assert_not_called()

    def test_backend_health_still_reports_jellyfin_outage(self):
        with patch('app.scraper.get_jellyfin_server_info', return_value={'connected': False}):
            response = self.client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['status'], 'degraded')

    def test_sigterm_runs_worker_and_selenium_cleanup(self):
        runtime = Mock()
        runtime.extensions = {'poster_jobs': self.queue}
        runtime.run.side_effect = lambda **kwargs: app_module.handle_shutdown_signal(app_module.signal.SIGTERM, None)
        with patch('app.create_app', return_value=runtime), \
                patch('app.logging.basicConfig'), patch('app.logging.FileHandler'), \
                patch('app.os.makedirs'), patch('app.signal.signal') as signal_handler, \
                patch.object(self.queue, 'close') as close, \
                patch('app.scraper.teardown_selenium') as teardown:
            with self.assertRaises(SystemExit) as stopped:
                app_module.main()
        self.assertEqual(stopped.exception.code, 0)
        close.assert_called_once()
        teardown.assert_called_once()
        self.assertEqual(signal_handler.call_count, 2)
        self.assertEqual(signal_handler.call_args_list[0].args, (app_module.signal.SIGTERM, app_module.handle_shutdown_signal))

    def test_proxy_rejects_foreign_origin_without_request(self):
        with patch('app.scraper.requests.get') as get:
            response = self.client.get('/jellyfin-image?url=https://example.invalid/image')
        self.assertEqual(response.status_code, 400)
        get.assert_not_called()

    def test_partial_upload_is_not_processed_and_retry_preserves_target(self):
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        with patch.object(self.service, 'upload', side_effect=[True, False]):
            self.queue.run(job['job_id'])
        self.assertEqual(self.store.processed_items(), [])
        failed = self.store.failed_items()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['failed_targets'][0]['target_id'], 'season')
        retry = self.queue.submit('retry', tasks=self.service.retry_tasks(['show']))
        with patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(retry['job_id'])
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(upload.call_args.args[0]['url'], SELECTION['season_posters']['season']['url'])
        self.assertEqual(self.store.failed_items(), [])
        self.assertEqual(len(self.store.processed_items()), 1)

    def test_cancel_after_upload_records_completed_target(self):
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        def cancel_after_upload(target):
            self.queue.cancel(job['job_id'])
            return True
        with patch.object(self.service, 'upload', side_effect=cancel_after_upload) as upload:
            self.queue.run(job['job_id'])
        result = self.store.get_job(job['job_id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(result['tasks'][0]['targets'][0]['status'], 'success')
        self.assertTrue(result['results'][0]['uploaded_any'])
        self.assertEqual(result['processed'], 0)  # The item's season is still unfinished.
        self.assertEqual(result['completed_targets'], 1)
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(self.store.processed_items(), [])

    def test_selections_survive_home_page_and_reopening_store(self):
        self.store.set_selection(ITEM, SELECTION)
        with patch('app.scraper.get_jellyfin_server_info', return_value={'name': 'Test'}), patch('app.scraper.get_jellyfin_libraries', return_value=[]), patch('app.scraper.get_jellyfin_items', return_value=[ITEM]):
            self.assertEqual(self.client.get('/').status_code, 200)
        reopened = StateStore(self.store.path)
        self.assertEqual(reopened.selections()['show']['selection'], SELECTION)

    def test_empty_scoped_clear_does_not_clear_all(self):
        self.store.record_target(ITEM, {'target_id': 'show', 'title': 'Primary', 'url': SELECTION['series_poster_url'], 'status': 'success'}, 'manual')
        response = self.client.delete('/processed-items', json={'item_ids': []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.processed_items()), 1)

    def test_recovery_requires_explicit_resume_and_keeps_completed_targets(self):
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        job['status'] = 'running'
        self.store.save_job(job)
        recovered = JobQueue(self.store, self.service, start_worker=False)
        self.assertEqual(self.store.get_job(job['job_id'])['status'], 'interrupted')
        with patch.object(self.service, 'upload', return_value=True) as upload:
            recovered.run(job['job_id'])
        upload.assert_not_called()
        recovered.resume(job['job_id'])
        with patch.object(self.service, 'upload', return_value=True):
            recovered.run(job['job_id'])
        self.assertEqual(self.store.get_job(job['job_id'])['status'], 'completed')

    def test_legacy_migration_is_idempotent_and_keeps_files(self):
        legacy = os.path.join(self.temp.name, 'results.log')
        entry = {'item_id': 'show', 'item_title': 'Example', 'status': 'success', 'poster_url': SELECTION['series_poster_url']}
        with open(legacy, 'w') as output:
            output.write(json.dumps(entry) + '\n')
        config = {'RESULTS_LOG_FILE': legacy}
        self.store.import_legacy(config)
        self.store.import_legacy(config)
        self.assertEqual(len(self.store.processed_items()), 1)
        with open(legacy) as original:
            self.assertEqual(json.loads(original.read()), entry)


if __name__ == '__main__':
    unittest.main()
