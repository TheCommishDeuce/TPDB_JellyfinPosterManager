import gzip
import re
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import app as app_module
import poster_scraper as scraper
from tests.test_workflows import ITEM, OfflineCase

LIBRARY = {'id': 'lib', 'name': 'Shows', 'type': 'tvshows'}
OTHER = dict(ITEM, id='other', title='Another')


def json_response(body):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


class JellyfinListingTests(unittest.TestCase):
    def test_items_are_requested_in_one_large_lean_page(self):
        with patch.object(scraper.requests, 'get', return_value=json_response({'Items': [], 'TotalRecordCount': 0})) as get:
            scraper.get_jellyfin_items(libraries=[{'id': 'lib', 'name': 'Movies', 'type': 'movies'}])
        query = parse_qs(urlsplit(get.call_args.args[0]).query)
        self.assertEqual(query['Limit'], [str(scraper.JELLYFIN_PAGE_SIZE)])
        self.assertGreaterEqual(scraper.JELLYFIN_PAGE_SIZE, 10000)
        self.assertNotIn('Path', query['Fields'][0].split(','))
        self.assertEqual(query['EnableImageTypes'], ['Primary'])
        self.assertEqual(query['EnableUserData'], ['false'])

    def test_libraries_are_fetched_in_parallel_and_merged_in_library_order(self):
        both_in_flight = threading.Barrier(2, timeout=2)
        pages = {
            'movies': [{'Id': 'x', 'Name': 'X', 'Type': 'Movie'}, {'Id': 'dup', 'Name': 'Dup', 'Type': 'Movie'}],
            'shows': [{'Id': 'dup', 'Name': 'Dup', 'Type': 'Movie'}, {'Id': 'z', 'Name': 'Z', 'Type': 'Series'}],
        }

        def get(url, **_kwargs):
            both_in_flight.wait()  # Only returns if the other library's request is running too.
            return json_response({'Items': pages[parse_qs(urlsplit(url).query)['ParentId'][0]]})

        libraries = [{'id': 'movies', 'name': 'Movies', 'type': 'movies'},
                     {'id': 'shows', 'name': 'Shows', 'type': 'tvshows'}]
        with patch.object(scraper.requests, 'get', side_effect=get):
            items = scraper.get_jellyfin_items(sort_by=None, libraries=libraries)
        self.assertEqual([item['id'] for item in items], ['x', 'dup', 'z'])
        self.assertEqual(items[1]['library_name'], 'Movies')

    def test_sort_none_keeps_jellyfin_order(self):
        items = [{'title': 'B'}, {'title': 'A'}]
        self.assertEqual(scraper.sort_jellyfin_items(list(items), None), items)
        self.assertEqual(scraper.sort_jellyfin_items(list(items), 'name'), items[::-1])

    def test_successful_upload_bumps_upload_count(self):
        response = Mock(status_code=204, text='')
        with patch.object(scraper, 'are_images_identical', return_value=False), \
                patch.object(scraper.requests, 'post', return_value=response), \
                patch.object(scraper, 'jellyfin_upload_count', 3), \
                patch('builtins.open', unittest.mock.mock_open(read_data=b'img')), \
                patch.object(scraper.os.path, 'exists', return_value=True):
            self.assertTrue(scraper.upload_image_to_jellyfin_improved('item', 'poster.jpg'))
            self.assertEqual(scraper.jellyfin_upload_count, 4)
            self.assertEqual(scraper.jellyfin_uploaded_items['item'], 4)


class HomePageCacheTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        for patcher in (
            patch('app.time.monotonic', side_effect=lambda: self.now),
            patch.object(scraper, 'get_jellyfin_server_info', return_value={'name': 'Test'}),
            patch.object(scraper, 'get_jellyfin_libraries', return_value=[LIBRARY]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fetch = patch.object(scraper, 'get_jellyfin_items', return_value=[ITEM, OTHER])
        self.get_items = self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def home(self, query=''):
        response = self.client.get('/' + query)
        self.assertEqual(response.status_code, 200)
        for thread in threading.enumerate():
            if thread.name == 'library-refresh':
                thread.join(timeout=5)
        return response.get_data(as_text=True)

    def test_reload_within_fresh_window_does_not_refetch(self):
        self.home()
        self.now += app_module.LIBRARY_FRESH_SEC - 1
        self.home()
        self.assertEqual(self.get_items.call_count, 1)
        self.assertIsNone(self.get_items.call_args.kwargs['sort_by'])

    def test_stale_listing_is_shown_and_refreshed_in_background(self):
        self.home()
        self.now += app_module.LIBRARY_FRESH_SEC + 1
        self.get_items.return_value = [ITEM]
        html = self.home()
        self.assertIn('Another', html)  # Served the cached listing...
        self.assertEqual(self.get_items.call_count, 2)  # ...and refreshed it.
        self.assertNotIn('Another', self.home())

    def test_listing_past_stale_limit_is_fetched_before_rendering(self):
        self.home()
        self.now += app_module.LIBRARY_STALE_SEC + 1
        self.get_items.return_value = [ITEM]
        self.assertNotIn('Another', self.home())
        self.assertEqual(self.get_items.call_count, 2)

    def test_upload_refreshes_that_card_without_refetching_the_listing(self):
        self.home()
        count = scraper.jellyfin_upload_count + 1
        with patch.object(scraper, 'jellyfin_upload_count', count), \
                patch.object(scraper, 'jellyfin_uploaded_items', {'show': count, 'season': count}):
            html = self.home()
        self.assertEqual(self.get_items.call_count, 1)
        self.assertIn(f'Items/show/Images/Primary%3FmaxWidth%3D300%26quality%3D85%26refresh%3D{count}', html)
        self.assertNotIn('Items/other/Images/Primary', html)  # Not uploaded: still the listing's (no poster).

    def test_type_and_library_filters_are_applied_in_the_page(self):
        film = dict(ITEM, id='film', title='Film', type='Movie', library_id='films', library_name='Films')
        show = dict(ITEM, library_id='lib', library_name='Shows')
        self.get_items.return_value = [film, show]
        hidden = lambda html: re.findall(r'class="[^"]*\bhidden\b[^"]*"[^>]*?data-(?:item|library)-id="([^"]+)"', html)
        self.assertEqual(sorted(hidden(self.home('?type=series'))), ['film', 'films'])
        self.assertEqual(sorted(hidden(self.home('?type=movies&library=films'))), ['lib', 'show'])
        self.assertEqual(sorted(hidden(self.home('?type=series&library=films'))), ['film', 'films'])  # Not a series library.
        self.assertEqual(hidden(self.home()), [])
        self.assertIn('<span id="visibleItemCount">1</span></strong><span id="itemCountTotalText"> of <strong>2</strong>',
                      self.home('?type=movies'))

    def test_cached_listing_is_sorted_per_request(self):
        html = self.home('?sort=name')
        self.assertLess(html.index('Another'), html.index('Example'))
        html = self.home('?sort=year')
        self.assertEqual(self.get_items.call_count, 1)

    def test_item_lookups_share_the_cached_listing(self):
        self.home()
        with patch.object(scraper, 'get_jellyfin_seasons', return_value=[]):
            self.client.get(f"/item/{ITEM['id']}/seasons")
        self.assertEqual(self.get_items.call_count, 1)

    def lookup(self, item_id):
        """Open an item the way the picker does; return which threads fetched the library."""
        fetched_by = []
        listing = self.get_items.return_value

        def fetch(**_kwargs):
            fetched_by.append(threading.current_thread().name)
            return listing
        self.get_items.side_effect = fetch
        with patch.object(scraper, 'get_jellyfin_seasons', return_value=[]):
            response = self.client.get(f"/item/{item_id}/seasons")
        for thread in threading.enumerate():
            if thread.name == 'library-refresh':
                thread.join(timeout=5)
        self.get_items.side_effect = None
        return response, fetched_by

    def test_item_lookup_after_fresh_window_does_not_wait_for_jellyfin(self):
        self.home()
        self.now += app_module.LIBRARY_FRESH_SEC + 1
        response, fetched_by = self.lookup(ITEM['id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetched_by, ['library-refresh'])

    def test_item_lookup_after_an_upload_does_not_wait_for_jellyfin(self):
        self.home()
        with patch.object(scraper, 'jellyfin_upload_count', scraper.jellyfin_upload_count + 1):
            response, fetched_by = self.lookup(ITEM['id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetched_by, [])

    def test_unknown_item_or_old_listing_is_fetched_before_answering(self):
        self.home()
        self.get_items.return_value = [ITEM, OTHER, dict(ITEM, id='new', title='Just Added')]
        self.now += app_module.LIBRARY_FRESH_SEC + 1
        response, fetched_by = self.lookup('new')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetched_by, [threading.current_thread().name])
        self.now += app_module.LIBRARY_STALE_SEC + 1
        response, fetched_by = self.lookup(ITEM['id'])
        self.assertEqual(fetched_by, [threading.current_thread().name])


class GzipTests(OfflineCase):
    def setUp(self):
        super().setUp()
        items = [dict(ITEM, id=f'item{i}', title=f'Title {i}') for i in range(200)]
        for patcher in (
            patch.object(scraper, 'get_jellyfin_server_info', return_value={'name': 'Test'}),
            patch.object(scraper, 'get_jellyfin_libraries', return_value=[LIBRARY]),
            patch.object(scraper, 'get_jellyfin_items', return_value=items),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_home_page_is_gzipped_when_accepted(self):
        plain = self.client.get('/')
        compressed = self.client.get('/', headers={'Accept-Encoding': 'gzip, deflate, br'})
        self.assertNotIn('Content-Encoding', plain.headers)
        self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
        self.assertIn('Accept-Encoding', compressed.headers['Vary'])
        self.assertEqual(gzip.decompress(compressed.data), plain.data)
        self.assertLess(len(compressed.data), len(plain.data) / 5)

    def test_large_json_is_gzipped_but_small_responses_and_images_are_not(self):
        json_response_ = self.client.get('/jellyfin-items', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(json_response_.headers['Content-Encoding'], 'gzip')
        small = self.client.get('/selections', headers={'Accept-Encoding': 'gzip'})
        self.assertNotIn('Content-Encoding', small.headers)
        with patch.object(scraper, 'fetch_tpdb_image', return_value=(b'\xff\xd8' + b'0' * 5000, 'image/jpeg')):
            image = self.client.get('/thumbnail?url=https://images.theposterdb.com/a.jpg',
                                    headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(image.status_code, 200)
        self.assertNotIn('Content-Encoding', image.headers)


class JobListTests(OfflineCase):
    def _job_with_results(self, count):
        job = self.queue.submit('auto', options={'filter': 'no-poster'})
        job.update(results=[{'item_id': f'item{n}', 'primary_uploaded': True} for n in range(count)], done=True)
        self.store.save_job(job)
        return job['job_id']

    def test_job_list_leaves_results_out(self):
        self._job_with_results(500)
        [job] = self.client.get('/jobs').get_json()['jobs']
        self.assertNotIn('results', job)
        self.assertEqual(job['result_count'], 500)

    def test_results_load_per_job_from_an_offset(self):
        job_id = self._job_with_results(3)
        results = self.client.get(f'/jobs/{job_id}/results?start=1').get_json()['results']
        self.assertEqual([result['item_id'] for result in results], ['item1', 'item2'])
        self.assertEqual(len(self.client.get(f'/jobs/{job_id}/results').get_json()['results']), 3)
        self.assertEqual(self.client.get('/jobs/missing/results').status_code, 404)


if __name__ == '__main__':
    unittest.main()
