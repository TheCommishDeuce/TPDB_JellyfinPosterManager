import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

import poster_scraper as scraper
from tests.test_workflows import ITEM, OfflineCase


class _ImmediateWait:
    """WebDriverWait stand-in: check the condition once instead of polling for seconds."""

    def __init__(self, driver, _timeout):
        self.driver = driver

    def until(self, condition):
        value = condition(self.driver)
        if not value:
            raise TimeoutException()
        return value


def _login_page_driver(page_errors=()):
    """A browser that never leaves /login after the form is submitted."""
    driver = Mock()
    driver.current_url = 'https://theposterdb.com/login'
    driver.page_source = '<html><head><title>Login</title></head></html>'
    driver.find_elements.return_value = [Mock(text=text) for text in page_errors]
    return driver


class TPDBLoginTests(unittest.TestCase):
    def setUp(self):
        scraper.selenium_driver = None
        self.addCleanup(setattr, scraper, 'selenium_driver', None)

    def _credentials(self):
        return patch.multiple(scraper.Config, TPDB_EMAIL='me@example.test', TPDB_PASSWORD='secret')

    def test_missing_credentials_fail_before_starting_chrome(self):
        with patch.multiple(scraper.Config, TPDB_EMAIL='', TPDB_PASSWORD=''), \
                patch.object(scraper, '_start_chrome') as start:
            with self.assertRaisesRegex(scraper.TPDBLoginFailed, 'TPDB_EMAIL and TPDB_PASSWORD'):
                scraper.setup_selenium_and_login()
        start.assert_not_called()

    def test_rejected_credentials_name_the_settings_and_tpdb_reason(self):
        driver = _login_page_driver(['These credentials do not match our records.'])
        with self._credentials(), patch.object(scraper, '_start_chrome', return_value=driver), \
                patch.object(scraper, 'WebDriverWait', _ImmediateWait):
            with self.assertRaises(scraper.TPDBLoginFailed) as raised:
                scraper.setup_selenium_and_login()
        message = str(raised.exception)
        self.assertIn('Check TPDB_EMAIL and TPDB_PASSWORD', message)
        self.assertIn('These credentials do not match our records.', message)
        self.assertIsNone(scraper.selenium_driver)

    def test_rejected_credentials_without_page_error_still_explain(self):
        with self._credentials(), patch.object(scraper, '_start_chrome', return_value=_login_page_driver()), \
                patch.object(scraper, 'WebDriverWait', _ImmediateWait):
            with self.assertRaisesRegex(scraper.TPDBLoginFailed, r'still on the sign-in page.*Check TPDB_EMAIL'):
                scraper.setup_selenium_and_login()


class TPDBLoginApiTests(OfflineCase):
    def test_posters_endpoint_reports_login_failure_as_503(self):
        problem = scraper.TPDBLoginFailed('ThePosterDB login failed. Check TPDB_EMAIL and TPDB_PASSWORD.')
        with patch.object(scraper, 'get_jellyfin_items', return_value=[ITEM]), \
                patch.object(scraper, 'get_jellyfin_seasons', return_value=[]), \
                patch.object(scraper, 'search_tpdb_for_poster_groups', side_effect=problem):
            response = self.client.get(f"/item/{ITEM['id']}/posters?use_cache=false")
        self.assertEqual(response.status_code, 503)
        self.assertIn('Check TPDB_EMAIL', response.get_json()['error'])

    def test_auto_job_stops_on_first_login_failure(self):
        problem = scraper.TPDBLoginFailed('ThePosterDB login failed. Check TPDB_EMAIL and TPDB_PASSWORD.')
        job = self.queue.submit('auto', options={'filter': 'all'})
        with patch('jobs.scraper.get_jellyfin_items', return_value=[ITEM, dict(ITEM, id='other')]), \
                patch.object(self.service, 'discover', side_effect=problem) as discover:
            self.queue.run(job['job_id'])
        finished = self.store.get_job(job['job_id'])
        self.assertEqual(finished['status'], 'failed')
        self.assertIn('Check TPDB_EMAIL', finished['message'])
        self.assertEqual(discover.call_count, 1)


if __name__ == '__main__':
    unittest.main()
