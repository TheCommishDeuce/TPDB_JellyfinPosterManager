import os
import threading
import unittest
from io import BytesIO
from unittest.mock import patch

import requests
from PIL import Image

import poster_scraper as scraper
from tests.test_workflows import OfflineCase

IMAGE_URL = 'https://images.theposterdb.com/prod/public/images/posters/optimized/movies/1/poster.jpg'


def jpeg_bytes():
    buffer = BytesIO()
    Image.new('RGB', (4, 6), 'navy').save(buffer, format='JPEG')
    return buffer.getvalue()


def rate_limited(retry_after='5'):
    response = requests.Response()
    response.status_code = 429
    response.headers['Retry-After'] = retry_after
    return requests.HTTPError('429 Too Many Requests', response=response)


class ChromeImageTests(unittest.TestCase):
    def test_scraper_browser_does_not_download_images(self):
        started = []

        def start(options, _binary):
            started.append(options)
            raise scraper.BrowserUnavailable('stop here')
        with patch.object(scraper, 'selenium_driver', None), patch.object(scraper, '_chrome_binary', return_value=None), \
                patch.object(scraper, '_start_chrome', side_effect=start):
            with self.assertRaises(scraper.BrowserUnavailable):
                scraper.setup_selenium_and_login()
        prefs = started[0].experimental_options['prefs']
        self.assertEqual(prefs['profile.managed_default_content_settings.images'], 2)


class TPDbImagePacingTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.clock_lock = threading.Lock()
        self.sleeps = []

        def sleep(seconds):
            with self.clock_lock:
                self.sleeps.append(round(seconds, 3))
                self.clock += seconds
        for patcher in (
            patch.object(scraper.time, 'monotonic', side_effect=lambda: self.clock),
            patch.object(scraper.time, 'sleep', side_effect=sleep),
            patch.object(scraper, 'tpdb_image_slots', threading.BoundedSemaphore(scraper.TPDB_IMAGE_MAX_CONCURRENT)),
            patch.object(scraper, 'tpdb_image_next_start', 0.0),
            patch.object(scraper, 'tpdb_image_paused_until', 0.0),
            patch.object(scraper, 'get_tpdb_cookie_jar', return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_image_requests_are_spaced_and_at_most_two_run_at_once(self):
        in_flight, peak, starts = [0], [0], []
        both_running = threading.Barrier(2, timeout=2)
        lock = threading.Lock()

        def fetch(url, *_args, **_kwargs):
            with lock:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
                starts.append(self.clock)
            try:
                both_running.wait()  # Hold two requests open together; a third must not start.
            except threading.BrokenBarrierError:
                pass
            with lock:
                in_flight[0] -= 1
            return b'img', 'image/jpeg'
        with patch.object(scraper, 'fetch_image', side_effect=fetch):
            threads = [threading.Thread(target=scraper.fetch_tpdb_image, args=(IMAGE_URL,)) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(len(starts), 6)
        self.assertEqual(peak[0], scraper.TPDB_IMAGE_MAX_CONCURRENT)
        gaps = [later - earlier for earlier, later in zip(sorted(starts), sorted(starts)[1:])]
        self.assertTrue(all(gap >= scraper.TPDB_IMAGE_MIN_INTERVAL_SEC - 1e-9 for gap in gaps), gaps)

    def test_upload_downloads_are_spaced_further_apart(self):
        with patch.object(scraper, 'fetch_image', return_value=(jpeg_bytes(), 'image/jpeg')) as fetch:
            for name in ('a.jpg', 'b.jpg'):
                self.assertTrue(scraper.download_image_with_cookies(IMAGE_URL, os.path.join(self.temp.name, name)))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.sleeps, [scraper.TPDB_UPLOAD_IMAGE_INTERVAL_SEC])

    def test_rate_limit_pauses_thumbnails_and_uploads_wait_it_out(self):
        with patch.object(scraper, 'fetch_image', side_effect=rate_limited('5')):
            response = self.client.get('/thumbnail?url=' + IMAGE_URL)
        self.assertEqual(response.status_code, 429)
        with patch.object(scraper, 'fetch_image', return_value=(jpeg_bytes(), 'image/jpeg')) as fetch:
            # During the pause a thumbnail fails at once, without asking TPDb...
            self.assertEqual(self.client.get('/thumbnail?url=' + IMAGE_URL).status_code, 429)
            fetch.assert_not_called()
            # ...while an upload waits for the pause to end, then downloads.
            self.assertTrue(scraper.download_image_with_cookies(IMAGE_URL, os.path.join(self.temp.name, 'p.jpg')))
            self.assertEqual(fetch.call_count, 1)
            self.assertGreaterEqual(sum(self.sleeps), 5)
            # Once the pause is over, thumbnails load again.
            self.assertEqual(self.client.get('/thumbnail?url=' + IMAGE_URL).status_code, 200)

    def test_rate_limited_upload_reports_the_rate_limit(self):
        with patch.object(scraper, 'fetch_image', side_effect=rate_limited('bogus')):
            with self.assertRaises(scraper.TPDBRateLimited):
                scraper.download_image_with_cookies(IMAGE_URL, os.path.join(self.temp.name, 'p.jpg'))
        self.assertEqual(scraper.tpdb_image_paused_until, self.clock + scraper.TPDB_IMAGE_RATE_LIMIT_PAUSE_SEC)


if __name__ == '__main__':
    unittest.main()
