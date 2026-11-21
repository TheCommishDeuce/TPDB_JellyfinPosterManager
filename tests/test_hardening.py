import unittest
from unittest.mock import MagicMock, patch

import requests

import app as app_module
import poster_scraper as scraper

TMDB_KEY = 'fixture-tmdb-key-0123456789'


class DebugModeTests(unittest.TestCase):
    def test_debug_setting_never_enables_flask_debug_mode(self):
        flask_app = MagicMock()
        with patch.object(app_module.Config, 'DEBUG', True), \
                patch.object(app_module.Config, 'LOG_DIR', 'logs', create=True), \
                patch.object(app_module.os, 'makedirs'), \
                patch.object(app_module.logging, 'basicConfig'), \
                patch.object(app_module.logging, 'FileHandler'), \
                patch.object(app_module.signal, 'signal'), \
                patch.object(app_module.scraper, 'teardown_selenium'), \
                patch.object(app_module, 'create_app', return_value=flask_app):
            app_module.main()
        self.assertIs(flask_app.run.call_args.kwargs['debug'], False)


class TmdbKeyTests(unittest.TestCase):
    def lookup_logs(self, error):
        def get(url, params=None, **_kwargs):
            # Build the error the way requests does: its message carries the full URL.
            prepared = requests.Request('GET', url, params=params).prepare()
            self.assertNotIn(TMDB_KEY, url)  # The key travels as a parameter, not in the format string.
            raise error(prepared.url)
        with patch.object(scraper.Config, 'TMDB_API_KEY', TMDB_KEY), \
                patch.object(scraper.requests, 'get', side_effect=get), \
                self.assertLogs(level='WARNING') as logs:
            query = scraper._resolve_tpdb_search_query('Abigail', item_type='Movie', tmdb_id='1111873')
        self.assertEqual(query, 'Abigail')
        return '\n'.join(logs.output)

    def test_http_error_log_does_not_contain_the_tmdb_key(self):
        def http_error(url):
            response = requests.Response()
            response.status_code, response.reason, response.url = 401, 'Unauthorized', url
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                return error
        output = self.lookup_logs(http_error)
        self.assertIn('401 Client Error', output)
        self.assertNotIn(TMDB_KEY, output)
        self.assertIn('<redacted>', output)

    def test_connection_error_log_does_not_contain_the_tmdb_key(self):
        output = self.lookup_logs(lambda url: requests.ConnectionError(f'Max retries exceeded with url: {url}'))
        self.assertNotIn(TMDB_KEY, output)


if __name__ == '__main__':
    unittest.main()
