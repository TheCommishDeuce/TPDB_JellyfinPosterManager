import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from selenium.common.exceptions import WebDriverException

import poster_scraper as scraper
from tests.test_workflows import ITEM, OfflineCase


def _clean_env():
    return patch.dict(os.environ, {key: '' for key in ('CHROME_BINARY', 'CHROMEDRIVER_PATH', 'SE_CHROMEDRIVER')})


class BrowserSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.driver_path = os.path.join(self.temp.name, 'chromedriver')
        with open(self.driver_path, 'w') as handle:
            handle.write('#!/bin/sh\n')
        os.chmod(self.driver_path, stat.S_IRWXU)

    def test_configured_driver_path_is_used(self):
        for variable in ('CHROMEDRIVER_PATH', 'SE_CHROMEDRIVER'):
            with self.subTest(variable=variable), _clean_env(), patch.dict(os.environ, {variable: self.driver_path}):
                self.assertEqual(scraper._chrome_service().path, self.driver_path)

    def test_without_configuration_selenium_manager_picks_driver(self):
        with _clean_env(), patch.object(scraper.Config, 'CHROMEDRIVER_PATH', '', create=True):
            with patch.object(scraper, 'Service') as service:
                scraper._chrome_service()
        self.assertNotIn('executable_path', service.call_args.kwargs)

    def test_invalid_paths_name_the_setting(self):
        missing = os.path.join(self.temp.name, 'missing')
        with _clean_env(), patch.dict(os.environ, {'CHROMEDRIVER_PATH': missing}):
            with self.assertRaisesRegex(scraper.BrowserUnavailable, 'CHROMEDRIVER_PATH=.*missing'):
                scraper._chrome_service()
        with _clean_env(), patch.dict(os.environ, {'CHROME_BINARY': missing}):
            with self.assertRaisesRegex(scraper.BrowserUnavailable, 'CHROME_BINARY=.*missing'):
                scraper._chrome_binary()

    def test_driver_start_failure_becomes_browser_unavailable(self):
        error = WebDriverException('session not created: This version of ChromeDriver only supports Chrome version 114')
        with _clean_env(), patch.object(scraper.webdriver, 'Chrome', side_effect=error):
            with self.assertRaisesRegex(scraper.BrowserUnavailable, 'only supports Chrome version 114'):
                scraper._start_chrome(scraper.Options(), None)


class FakeBrowser:
    def __init__(self, user_agent, error=None):
        self.hints = {
            'userAgent': user_agent, 'brands': [{'brand': 'Chromium', 'version': '156'}],
            'fullVersionList': [{'brand': 'Chromium', 'version': '156.0.8078.79'}], 'platform': 'Linux',
            'platformVersion': '6.6.0', 'architecture': 'x86', 'model': '', 'mobile': False, 'bitness': '64', 'wow64': False,
        }
        self.error, self.visited, self.cdp = error, [], []

    def get(self, url):
        self.visited.append(url)

    def execute_script(self, _script):
        if self.error:
            raise self.error
        return dict(self.hints)

    def execute_cdp_cmd(self, command, params):
        self.cdp.append((command, params))


class UserAgentTests(unittest.TestCase):
    HEADLESS = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36'

    def test_user_agent_is_the_real_browser_without_the_headless_marker(self):
        browser = FakeBrowser(self.HEADLESS)
        scraper._match_user_agent_to_browser(browser)
        self.assertEqual(browser.visited, ['chrome://version'])  # Local page: nothing is sent to TPDb.
        (command, params), = browser.cdp
        self.assertEqual(command, 'Network.setUserAgentOverride')
        self.assertEqual(params['userAgent'], self.HEADLESS.replace('HeadlessChrome', 'Chrome'))
        # Client hints keep the real browser's values; without them navigator.userAgentData goes blank.
        metadata = params['userAgentMetadata']
        self.assertEqual(metadata['brands'], [{'brand': 'Chromium', 'version': '156'}])
        self.assertEqual((metadata['platform'], metadata['fullVersionList'][0]['version']), ('Linux', '156.0.8078.79'))
        self.assertNotIn('userAgent', metadata)

    def test_a_non_headless_user_agent_is_left_alone(self):
        browser = FakeBrowser(self.HEADLESS.replace('HeadlessChrome', 'Chrome'))
        scraper._match_user_agent_to_browser(browser)
        self.assertEqual(browser.cdp, [])

    def test_failure_to_read_the_user_agent_is_not_fatal(self):
        browser = FakeBrowser(self.HEADLESS, error=WebDriverException('javascript error'))
        with self.assertLogs(level='WARNING'):
            scraper._match_user_agent_to_browser(browser)
        self.assertEqual(browser.cdp, [])

    def test_no_fixed_user_agent_is_passed_to_chrome(self):
        started = []

        def start(options, _binary):
            started.append(options)
            raise scraper.BrowserUnavailable('stop here')
        with patch.object(scraper, 'selenium_driver', None), patch.object(scraper, '_chrome_binary', return_value=None), \
                patch.object(scraper.Config, 'TPDB_EMAIL', 'fixture@example.invalid', create=True), \
                patch.object(scraper.Config, 'TPDB_PASSWORD', 'fixture-password', create=True), \
                patch.object(scraper, '_start_chrome', side_effect=start):
            with self.assertRaises(scraper.BrowserUnavailable):
                scraper.setup_selenium_and_login()
        self.assertFalse([argument for argument in started[0].arguments if argument.startswith('--user-agent')])


class BrowserUnavailableApiTests(OfflineCase):
    def test_posters_endpoint_reports_browser_problem_as_503(self):
        problem = scraper.BrowserUnavailable('Could not start headless Chrome: no chromedriver')
        with patch.object(scraper, 'get_jellyfin_items', return_value=[ITEM]), \
                patch.object(scraper, 'get_jellyfin_seasons', return_value=[]), \
                patch.object(scraper, 'search_tpdb_for_poster_groups', side_effect=problem), \
                patch.object(scraper, 'setup_selenium_and_login', side_effect=problem):
            response = self.client.get(f"/item/{ITEM['id']}/posters?use_cache=false")
        self.assertEqual(response.status_code, 503)
        self.assertIn('no chromedriver', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
