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

    @patch.object(poster_scraper.requests, 'get')
    def test_collections_keep_their_boxset_type_and_request_the_collection_section(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Items': [
                {'Id': 'collection', 'Name': 'Example Collection', 'Type': 'BoxSet', 'ProductionYear': 2024},
                {'Id': 'collection', 'Name': 'Example Collection', 'Type': 'BoxSet', 'ProductionYear': 2024},
                {'Id': 'movie', 'Name': 'Example Movie', 'Type': 'Movie'},
                {'Id': 'folder', 'Name': 'Unexpected folder', 'Type': 'Folder'},
            ]
        }
        get.return_value = response

        with patch.object(poster_scraper.Config, 'JELLYFIN_URL', 'http://jellyfin'), \
             patch.object(poster_scraper.Config, 'JELLYFIN_API_KEY', 'test-key'):
            items = poster_scraper.get_jellyfin_items(
                libraries=[{'id': 'library-id', 'name': 'Library'}],
            )

        self.assertEqual(
            [(item['id'], item['type']) for item in items],
            [('collection', 'BoxSet'), ('movie', 'Movie')],
        )
        self.assertIn('IncludeItemTypes=Movie,Series,BoxSet', get.call_args.args[0])
        self.assertEqual(
            poster_scraper._build_tpdb_search_url('Example Collection', item_type='BoxSet'),
            'https://theposterdb.com/search?term=Example+Collection&section=collections',
        )


if __name__ == '__main__':
    unittest.main()
