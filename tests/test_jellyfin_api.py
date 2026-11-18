import base64
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import poster_scraper


class JellyfinApiCompatibilityTests(unittest.TestCase):
    def test_auth_header_uses_jellyfin_12_authorization_format(self):
        with patch.object(poster_scraper.Config, 'JELLYFIN_API_KEY', 'test-key'):
            headers = poster_scraper.get_jellyfin_headers(Accept='application/json')

        self.assertEqual(headers['Authorization'], 'MediaBrowser Token="test-key"')
        self.assertEqual(headers['Accept'], 'application/json')
        self.assertNotIn('X-Emby-Token', headers)

    @patch.object(poster_scraper, 'are_images_identical', return_value=False)
    @patch.object(poster_scraper.requests, 'post')
    def test_upload_sends_base64_image_body(self, post, _images_identical):
        post.return_value = Mock(status_code=204)
        image_bytes = b'\x89PNG\r\n\x1a\nraw-image-data'

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as image_file:
            image_file.write(image_bytes)
            image_path = image_file.name

        try:
            with patch.object(poster_scraper.Config, 'JELLYFIN_URL', 'http://jellyfin'):
                with patch.object(poster_scraper.Config, 'JELLYFIN_API_KEY', 'test-key'):
                    result = poster_scraper.upload_image_to_jellyfin_improved('item-id', image_path)
        finally:
            os.unlink(image_path)

        self.assertTrue(result)
        _, kwargs = post.call_args
        self.assertEqual(kwargs['data'], base64.b64encode(image_bytes))
        self.assertEqual(kwargs['headers']['Content-Type'], 'image/png')
        self.assertEqual(kwargs['headers']['Authorization'], 'MediaBrowser Token="test-key"')

    @patch.object(poster_scraper.requests, 'get')
    def test_date_added_sort_with_libraries(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Items': [
                {'Id': 'older', 'Name': 'Older', 'Type': 'Movie', 'DateCreated': '2024-01-01T00:00:00Z'},
                {'Id': 'missing', 'Name': 'Missing date', 'Type': 'Movie'},
                {'Id': 'newer', 'Name': 'Newer', 'Type': 'Series', 'DateCreated': '2025-01-01T00:00:00Z'},
            ]
        }
        get.return_value = response

        items = poster_scraper.get_jellyfin_items(
            sort_by='date_added',
            libraries=[{'id': 'library-id', 'name': 'Library'}],
        )

        self.assertEqual([item['id'] for item in items], ['newer', 'older', 'missing'])


    @staticmethod
    def _paged_server(total_items, cap, report_total=True):
        """Mimic a Jellyfin-compatible server (e.g. Silo) that caps each page at ``cap`` items."""
        from urllib.parse import parse_qs, urlsplit
        all_items = [{'Id': f'id{i:04d}', 'Name': f'Item {i:04d}', 'Type': 'Movie'} for i in range(total_items)]
        calls = []

        def get(url, **_kwargs):
            query = parse_qs(urlsplit(url).query)
            start = int(query.get('StartIndex', ['0'])[0])
            calls.append(start)
            page = all_items[start:start + cap]
            body = {'Items': page}
            if report_total:
                body['TotalRecordCount'] = total_items
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = body
            return response
        return get, calls

    def test_items_follow_pagination_when_server_caps_page_size(self):
        get, calls = self._paged_server(150, cap=72)
        with patch.object(poster_scraper.requests, 'get', side_effect=get):
            items = poster_scraper.get_jellyfin_items(libraries=[{'id': 'lib', 'name': 'Movies'}])
        self.assertEqual(len(items), 150)
        self.assertEqual(calls, [0, 72, 144])

    def test_items_paging_without_total_stops_on_empty_page(self):
        get, calls = self._paged_server(150, cap=72, report_total=False)
        with patch.object(poster_scraper.requests, 'get', side_effect=get):
            items = poster_scraper.get_jellyfin_items(libraries=[{'id': 'lib', 'name': 'Movies'}])
        self.assertEqual(len(items), 150)
        self.assertEqual(calls, [0, 72, 144, 150])

    def test_stock_jellyfin_full_response_needs_one_request(self):
        get, calls = self._paged_server(150, cap=10_000)
        with patch.object(poster_scraper.requests, 'get', side_effect=get):
            items = poster_scraper.get_jellyfin_items(item_type='movies', libraries=[])
        self.assertEqual(len(items), 150)
        self.assertEqual(calls, [0])

    def test_server_ignoring_start_index_does_not_loop(self):
        page = [{'Id': 'a', 'Name': 'A', 'Type': 'Movie'}, {'Id': 'b', 'Name': 'B', 'Type': 'Movie'}]
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'Items': page}
        with patch.object(poster_scraper.requests, 'get', return_value=response) as get:
            items = poster_scraper.get_jellyfin_items(item_type='movies', libraries=[])
        self.assertEqual([item['id'] for item in items], ['a', 'b'])
        self.assertEqual(get.call_count, 2)

if __name__ == '__main__':
    unittest.main()
