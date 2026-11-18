import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from PIL import Image
import poster_scraper as scraper
from safe_http import fetch_image, validate_image_url
from tests.test_workflows import ITEM, SELECTION, OfflineCase


class SecurityTests(unittest.TestCase):
    def test_foreign_redirect_is_checked_before_sending_credentials(self):
        response = Mock(status_code=302, headers={'Location': 'https://example.invalid/stolen'})
        with patch('safe_http.requests.get', return_value=response) as get:
            with self.assertRaises(ValueError):
                fetch_image('http://jellyfin.test/Items/show/Images/Primary', 'jellyfin',
                            headers={'Authorization': 'test-key'})
        self.assertEqual(get.call_count, 1)
        response.close.assert_called_once()

    def test_host_userinfo_port_and_path_bypasses_are_rejected(self):
        for url in (
            'http://jellyfin.test.evil/Items/show/Images/Primary',
            'http://jellyfin.test@evil/Items/show/Images/Primary',
            'http://jellyfin.test:123/Items/show/Images/Primary',
            'http://jellyfin.test/System/Info',
            'http://jellyfin.test/Items/../System/Images/Primary',
            'file:///etc/passwd',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_image_url(url, 'jellyfin')
        with self.assertRaises(ValueError):
            validate_image_url('https://evil.theposterdb.com/image', 'tpdb')

    def test_image_size_and_type_are_checked(self):
        response = Mock(status_code=200, headers={'Content-Type': 'text/html'})
        with patch('safe_http.requests.get', return_value=response), self.assertRaises(ValueError):
            fetch_image('https://theposterdb.com/image', 'tpdb')
        response.headers = {'Content-Type': 'image/jpeg'}
        response.iter_content.return_value = [b'123', b'456']
        with patch('safe_http.requests.get', return_value=response), patch('safe_http.MAX_IMAGE_BYTES', 5), self.assertRaises(ValueError):
            fetch_image('https://theposterdb.com/image', 'tpdb')

    def test_cookies_keep_domain_path_and_secure_flags(self):
        driver = Mock()
        driver.get_cookies.return_value = [
            {'name': 'session', 'value': 'secret', 'domain': '.theposterdb.com', 'path': '/api', 'secure': True},
            {'name': 'other', 'value': 'secret', 'domain': 'evil.test'},
        ]
        with patch.object(scraper, 'selenium_driver', driver):
            jar = scraper.get_tpdb_cookie_jar()
        self.assertEqual(len(jar), 1)
        cookie = next(iter(jar))
        self.assertEqual((cookie.domain, cookie.path, cookie.secure), ('.theposterdb.com', '/api', True))

    def test_download_actually_converts_png_to_jpeg(self):
        buffer = io.BytesIO()
        Image.new('RGBA', (3, 3), (255, 0, 0, 128)).save(buffer, format='PNG')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'poster.jpg')
            with patch.object(scraper, 'fetch_tpdb_image', return_value=(buffer.getvalue(), 'image/png')):
                self.assertTrue(scraper.download_image_with_cookies('https://theposterdb.com/image', path))
            with Image.open(path) as image:
                self.assertEqual(image.format, 'JPEG')

    def test_download_keeps_jpeg_bytes_unchanged(self):
        buffer = io.BytesIO()
        Image.new('RGB', (3, 3), (0, 128, 255)).save(buffer, format='JPEG', quality=80)
        original = buffer.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'poster.jpg')
            with patch.object(scraper, 'fetch_tpdb_image', return_value=(original, 'image/jpeg')):
                self.assertTrue(scraper.download_image_with_cookies('https://theposterdb.com/image', path))
            with open(path, 'rb') as handle:
                self.assertEqual(handle.read(), original)

    def test_download_rejects_undecodable_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'poster.jpg')
            with patch.object(scraper, 'fetch_tpdb_image', return_value=(b'\xff\xd8not-a-jpeg', 'image/jpeg')):
                self.assertFalse(scraper.download_image_with_cookies('https://theposterdb.com/image', path))

    def test_automatic_fuzzy_match_is_review_not_fallback_upload(self):
        driver = Mock()
        driver.current_url = 'https://theposterdb.com/search'
        driver.page_source = '''<a class="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative"
                               href="/posters/123"><span>Example Two (2020)</span></a>'''
        with patch.object(scraper, 'selenium_driver', driver), patch.object(scraper, '_wait_for_search_results_ready'):
            result = scraper.search_tpdb_for_poster_groups('Example', item_year=2020, item_type='Series',
                                                          include_base64=False, require_exact=True)
        self.assertTrue(result['review_needed'])
        self.assertIsNone(result['best_group'])
        self.assertEqual(driver.get.call_count, 1)


class IntegrationTests(OfflineCase):
    def test_manual_api_requires_confirmation_for_protected_items(self):
        self.store.set_selection(ITEM, SELECTION['series_poster_url'])
        self.store.set_protected('show', True)
        with patch('app.scraper.get_jellyfin_items', return_value=[ITEM]):
            response = self.client.post('/jobs', json={'kind': 'manual', 'item_ids': ['show']})
            self.assertEqual(response.status_code, 403)
            response = self.client.post('/jobs', json={'kind': 'manual', 'item_ids': ['show'], 'confirm_protected': True})
        self.assertEqual(response.status_code, 202)
        with patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(response.json['job_id'])
        upload.assert_called_once()

    def test_cross_site_and_rebinding_requests_are_rejected(self):
        self.assertEqual(self.client.post('/jobs', json={}, headers={'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/selections', headers={'Host': 'evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/selections', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)

    def test_successful_season_does_not_resolve_failed_primary(self):
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        with patch.object(self.service, 'upload', side_effect=[False, True]):
            self.queue.run(job['job_id'])
        self.assertEqual(self.store.processed_items(), [])
        failed = self.store.failed_items()[0]['failed_targets']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['target_id'], 'show')
        self.assertEqual(failed[0]['url'], SELECTION['series_poster_url'])

    def test_partial_upload_consumes_only_successful_queued_selection(self):
        self.store.set_selection(ITEM, SELECTION)
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        with patch.object(self.service, 'upload', side_effect=[True, False]):
            self.queue.run(job['job_id'])
        remaining = self.store.selections()['show']['selection']
        self.assertNotIn('series_poster_url', remaining)
        self.assertEqual(remaining['season_posters'], SELECTION['season_posters'])
        self.assertEqual(self.store.get_job(job['job_id'])['failed'], 1)

    def test_new_selection_is_not_consumed_by_older_job(self):
        self.store.set_selection(ITEM, 'https://theposterdb.com/api/assets/new')
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION['series_poster_url']}])
        with patch.object(self.service, 'upload', return_value=True):
            self.queue.run(job['job_id'])
        self.assertEqual(self.store.selections()['show']['selection'], 'https://theposterdb.com/api/assets/new')

    def test_retry_of_protected_item_does_not_upload(self):
        self.store.record_target(ITEM, {'target_id': 'show', 'title': 'Primary', 'url': SELECTION['series_poster_url'], 'status': 'failed'}, 'manual')
        self.store.set_protected('show', True)
        job = self.queue.submit('retry', tasks=self.service.retry_tasks(['show']))
        with patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(job['job_id'])
        upload.assert_not_called()
        self.assertEqual(self.store.get_job(job['job_id'])['skipped'], 1)
        self.assertEqual(len(self.store.failed_items()), 1)

    def test_cancel_then_resume_skips_recorded_success_and_reuses_urls(self):
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        def cancel(target):
            self.queue.cancel(job['job_id'])
            return True
        with patch.object(self.service, 'upload', side_effect=cancel):
            self.queue.run(job['job_id'])
        self.queue.resume(job['job_id'])
        with patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(job['job_id'])
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0]['target_id'], 'season')
        self.assertEqual(self.store.get_job(job['job_id'])['successful'], 1)

    def test_queue_serializes_concurrent_run_requests(self):
        jobs = [self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION['series_poster_url']}]) for _ in range(2)]
        gate = threading.Event()
        release = threading.Event()
        active = []
        def upload(target):
            active.append(threading.get_ident())
            gate.set()
            self.assertTrue(release.wait(2))
            return True
        with patch.object(self.service, 'upload', side_effect=upload), ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(self.queue.run, job['job_id']) for job in jobs]
            self.assertTrue(gate.wait(2))
            self.assertEqual(len(active), 1)
            release.set()
            for future in futures:
                future.result(timeout=3)
        self.assertEqual(len(active), 2)

    def test_concurrent_protection_updates_are_not_lost(self):
        with ThreadPoolExecutor(8) as executor:
            list(executor.map(lambda number: self.store.set_protected(str(number), True), range(40)))
        self.assertEqual(len(self.store.values('protected')), 40)

    def test_corrected_mapping_is_used_even_when_picker_cache_is_disabled(self):
        self.store.set_value('mapping', 'show', 'https://theposterdb.com/posters/123')
        search = Mock(return_value={'best_group': None, 'groups': [], 'posters': []})
        with patch('app.scraper.get_jellyfin_items', return_value=[ITEM]), patch('app.scraper.get_jellyfin_seasons', return_value=[]), patch('app.scraper.search_tpdb_for_poster_groups', search):
            response = self.client.get('/item/show/posters?use_cache=false')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_args.kwargs['tpdb_item_url'], 'https://theposterdb.com/posters/123')

    def test_auto_discovery_uses_mapping_and_disables_embedded_previews(self):
        self.store.set_value('mapping', 'show', 'https://theposterdb.com/posters/123')
        group = {'show_posters': [{'url': SELECTION['series_poster_url']}]}
        with patch('poster_service.scraper.search_tpdb_for_poster_groups', return_value={'best_group': group}) as search:
            targets = self.service.discover(ITEM, {})
        self.assertEqual(len(targets), 1)
        self.assertEqual(search.call_args.kwargs['tpdb_item_url'], 'https://theposterdb.com/posters/123')
        self.assertFalse(search.call_args.kwargs['include_base64'])

    def test_invalid_api_payloads_never_start_jobs(self):
        for data in ({'kind': 'auto', 'filter': 'unknown'}, {'kind': 'manual', 'item_ids': 'show'},
                     {'kind': 'auto', 'skip_processed': 'false'}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post('/jobs', json=data).status_code, 400)
        self.assertEqual(self.store.jobs(), [])

    def test_legacy_upload_routes_use_the_shared_worker(self):
        self.queue.start_worker = True
        self.addCleanup(self.queue.close)
        cases = [('/upload/show', {}), ('/upload-all', {}),
                 ('/upload-poster', {'item_id': 'show', 'poster_url': SELECTION['series_poster_url']})]
        with patch('app.scraper.get_jellyfin_items', return_value=[ITEM]), patch.object(self.service, 'upload', return_value=True) as upload:
            for path, data in cases:
                self.store.set_selection(ITEM, SELECTION['series_poster_url'])
                response = self.client.post(path, json=data)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertTrue(response.json['success'])
        self.assertEqual(upload.call_count, 3)
        self.assertEqual(len(self.store.jobs()), 3)

    def test_cancel_queued_job_never_uploads(self):
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        self.queue.cancel(job['job_id'])
        with patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(job['job_id'])
        upload.assert_not_called()
        self.assertEqual(self.store.get_job(job['job_id'])['status'], 'cancelled')

    def test_retry_stays_failed_until_it_actually_succeeds(self):
        self.store.record_target(ITEM, {'target_id': 'show', 'title': 'Primary', 'url': SELECTION['series_poster_url'], 'status': 'failed'}, 'manual')
        self.queue.submit('retry', tasks=self.service.retry_tasks(['show']))
        self.assertEqual(len(self.store.failed_items()), 1)
        self.assertEqual(self.store.processed_items(), [])

    def test_restart_resumes_only_pending_targets(self):
        from jobs import JobQueue
        job = self.queue.submit('manual', tasks=[{'item': ITEM, 'selection': SELECTION}])
        job['status'] = 'running'
        job['tasks'][0]['targets'][0]['status'] = 'success'
        self.store.save_job(job, (ITEM, job['tasks'][0]['targets'][0], 'manual'))
        recovered = JobQueue(self.store, self.service, start_worker=False)
        self.assertEqual(self.store.get_job(job['job_id'])['status'], 'interrupted')
        recovered.resume(job['job_id'])
        with patch.object(self.service, 'upload', return_value=True) as upload:
            recovered.run(job['job_id'])
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0]['target_id'], 'season')

    def test_second_worker_cannot_use_the_same_database(self):
        from jobs import JobQueue
        first = JobQueue(self.store, self.service)
        try:
            with self.assertRaises(RuntimeError):
                JobQueue(self.store, self.service)
        finally:
            first.close()

    def test_auto_filter_respects_library_and_protected(self):
        movie = dict(ITEM, id='movie', type='Movie', library_id='movies')
        protected = dict(movie, id='protected')
        self.store.set_protected('protected', True)
        job = self.queue.submit('auto', options={'filter': 'movies', 'library_id': 'movies'})
        with patch('jobs.scraper.get_jellyfin_items', return_value=[ITEM, movie, protected]), patch.object(self.service, 'discover', return_value=[{'target_id': 'movie', 'title': 'Primary', 'url': SELECTION['series_poster_url'], 'status': 'pending'}]), patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(job['job_id'])
        upload.assert_called_once()
        self.assertEqual(self.store.get_job(job['job_id'])['total_items'], 1)

    def test_expired_upload_login_retries_same_url_without_searching(self):
        from safe_http import ImageLoginRequired
        target = {'target_id': 'show', 'url': SELECTION['series_poster_url']}
        with patch.object(scraper.Config, 'TEMP_POSTER_DIR', self.temp.name), patch.object(scraper, 'setup_selenium_and_login') as login, patch.object(scraper, 'download_image_with_cookies', side_effect=[ImageLoginRequired('expired'), True]) as download, patch.object(scraper, 'upload_image_to_jellyfin_improved', return_value=True), patch.object(scraper, 'search_tpdb_for_poster_groups') as search:
            self.assertTrue(self.service.upload(target))
        self.assertEqual(login.call_count, 2)
        self.assertEqual(login.call_args.kwargs, {'force': True})
        self.assertEqual([call.args[0] for call in download.call_args_list], [target['url'], target['url']])
        search.assert_not_called()

    def test_invalid_saved_mapping_never_disables_exact_matching(self):
        self.store.set_value('mapping', 'show', 'https://evil.test/posters/1')
        with patch('poster_service.scraper.search_tpdb_for_poster_groups') as search:
            with self.assertRaises(ValueError):
                self.service.discover(ITEM, {})
        search.assert_not_called()

    def test_arbitrary_season_ids_are_rejected(self):
        self.store.set_selection(ITEM, SELECTION)
        with patch('app.scraper.get_jellyfin_items', return_value=[ITEM]), patch('app.scraper.get_jellyfin_seasons', return_value=[]):
            response = self.client.post('/jobs', json={'kind': 'manual', 'item_ids': ['show']})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.jobs(), [])
