import os
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException

import poster_scraper as scraper
from picker_cache import PickerCache
from tests.test_workflows import OfflineCase

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
BASE = 'https://theposterdb.com'
ADVANCED = BASE + '/search/advanced/results'
IMDB_URL = ADVANCED + '?category=Movies&imdb_id=tt27489557'
TMDB_URL = ADVANCED + '?category=Movies&tmdb_id=1111873'
TEXT_URL = BASE + '/search?term=Abigail&section=movies'
ITEM_URL = BASE + '/posters/1842083'
LOGIN_URL = BASE + '/login'


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as source:
        return source.read()


ITEM_PAGE = fixture('tpdb_advanced_item_redirect.html')
NO_RESULTS = fixture('tpdb_advanced_no_results.html')
RESULTS_LIST = fixture('tpdb_advanced_results_list.html')
TEXT_RESULTS = (
    '<html><head><title>Search | The Poster Database (TPDb)</title></head><body>'
    '<a class="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative" '
    'href="/posters/1842083">Abigail (2024)</a></body></html>'
)


def results_page(page, has_next=True):
    next_link = f'<a rel="next" href="{ADVANCED}?category=Movies&imdb_id=tt1&page={page + 1}">Next</a>' if has_next else ''
    return (
        '<html><head><title>Search | The Poster Database (TPDb)</title></head><body>'
        '<a class="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative" '
        f'href="/posters/{9000 + page}">Result {page} (2019)</a>{next_link}</body></html>'
    )


class FakeDriver:
    """Serves canned pages by URL: a page is (final_url, html) to mimic redirects, or an exception."""

    def __init__(self, pages):
        self.pages = pages
        self.visited = []
        self.page_load_timeouts = []
        self.current_url = 'about:blank'
        self.page_source = ''

    def get(self, url):
        self.visited.append(url)
        if url not in self.pages:
            raise AssertionError(f'Unexpected TPDb page load: {url}')
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        self.current_url, self.page_source = page

    def find_elements(self, by, selector):
        return BeautifulSoup(self.page_source, 'html.parser').select(selector)

    def set_page_load_timeout(self, seconds):
        self.page_load_timeouts.append(seconds)


class TPDbIdSearchCase(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket.connect', side_effect=AssertionError('Network is disabled in tests'))
        network.start()
        self.addCleanup(network.stop)
        for patcher in (
            patch.object(scraper.time, 'sleep'),
            patch.object(scraper, 'tpdb_advanced_search_available', True),
            patch.object(scraper, '_resolve_tpdb_search_query', side_effect=lambda title, **kwargs: title),
            patch.object(scraper.Config, 'TPDB_BASE_URL', BASE),
            patch.object(scraper.Config, 'TPDB_SEARCH_URL_TEMPLATE', BASE + '/search?term={query}'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_driver(self, pages):
        driver = FakeDriver(pages)
        patcher = patch.object(scraper, 'selenium_driver', driver)
        patcher.start()
        self.addCleanup(patcher.stop)
        return driver

    def search(self, **kwargs):
        options = dict(item_year=2024, item_type='Movie', include_base64=False)
        options.update(kwargs)
        return scraper.search_tpdb_for_poster_groups('Abigail', **options)


class IdSearchFlowTests(TPDbIdSearchCase):
    def test_imdb_hit_skips_text_search(self):
        driver = self.use_driver({IMDB_URL: (ITEM_URL, ITEM_PAGE)})
        result = self.search(imdb_id='tt27489557', tmdb_id='1111873')
        # The redirect already landed on the item page, so it is parsed without a second load.
        self.assertEqual(driver.visited, [IMDB_URL])
        self.assertEqual(result['best_group']['url'], ITEM_URL)
        self.assertEqual(result['best_group']['title'], 'Abigail (2024)')
        self.assertTrue(result['posters'])
        self.assertEqual(result['posters'][0]['uploader'], 'TheBigG')

    def test_imdb_miss_falls_through_to_tmdb_hit(self):
        driver = self.use_driver({IMDB_URL: (IMDB_URL, NO_RESULTS), TMDB_URL: (ITEM_URL, ITEM_PAGE)})
        result = self.search(imdb_id='tt27489557', tmdb_id='1111873')
        self.assertEqual(driver.visited, [IMDB_URL, TMDB_URL])
        self.assertEqual(scraper.time.sleep.call_count, 1)  # Spacing between the two ID page loads.
        self.assertEqual(result['best_group']['url'], ITEM_URL)

    def test_both_ids_miss_runs_unchanged_text_search(self):
        pages = {TEXT_URL: (TEXT_URL, TEXT_RESULTS), ITEM_URL: (ITEM_URL, ITEM_PAGE)}
        text_only = self.use_driver(dict(pages))
        expected = self.search()
        driver = self.use_driver(dict(pages, **{IMDB_URL: (IMDB_URL, NO_RESULTS), TMDB_URL: (TMDB_URL, NO_RESULTS)}))
        result = self.search(imdb_id='tt27489557', tmdb_id='1111873')
        self.assertEqual(driver.visited, [IMDB_URL, TMDB_URL, TEXT_URL, ITEM_URL])
        self.assertEqual(text_only.visited, [TEXT_URL, ITEM_URL])
        self.assertEqual(result, expected)
        self.assertEqual(result['best_group']['url'], ITEM_URL)

    def test_no_ids_runs_text_search_only(self):
        driver = self.use_driver({TEXT_URL: (TEXT_URL, TEXT_RESULTS), ITEM_URL: (ITEM_URL, ITEM_PAGE)})
        result = self.search(imdb_id=None, tmdb_id=None)
        self.assertEqual(driver.visited, [TEXT_URL, ITEM_URL])
        self.assertEqual(result['best_group']['url'], ITEM_URL)

    def test_login_redirect_raises_session_expired_like_text_search(self):
        driver = self.use_driver({IMDB_URL: (LOGIN_URL, '<html><title>Login</title></html>')})
        with patch.object(scraper, 'setup_selenium_and_login') as login:
            with self.assertRaises(scraper.TPDBSessionExpired):
                self.search(imdb_id='tt27489557')
        login.assert_called_once_with(force=True)
        self.assertEqual(driver.visited, [IMDB_URL, IMDB_URL])

    def test_several_id_matches_need_review_in_batch_mode(self):
        self.use_driver({IMDB_URL: (IMDB_URL, results_page(1, has_next=False).replace(
            '</body>', '<a class="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative" '
                       'href="/posters/2">Other (2019)</a></body>'))})
        result = self.search(imdb_id='tt27489557', require_exact=True)
        self.assertTrue(result['review_needed'])

    def test_batch_mode_trusts_an_id_match_whose_title_agrees(self):
        self.use_driver({IMDB_URL: (ITEM_URL, ITEM_PAGE)})
        result = self.search(imdb_id='tt27489557', require_exact=True)
        self.assertEqual(result['best_group']['url'], ITEM_URL)

    def test_batch_mode_allows_an_id_match_one_year_off(self):
        for year in (2023, 2025):
            with self.subTest(year=year):
                self.use_driver({IMDB_URL: (ITEM_URL, ITEM_PAGE)})
                result = self.search(imdb_id='tt27489557', item_year=year, require_exact=True)
                self.assertEqual(result['best_group']['url'], ITEM_URL)

    def test_batch_mode_reviews_an_id_match_for_another_title_or_year(self):
        for title, year in (('Some Other Film', 2024), ('Abigail', 2019)):
            with self.subTest(title=title, year=year):
                self.use_driver({IMDB_URL: (ITEM_URL, ITEM_PAGE)})
                result = scraper.search_tpdb_for_poster_groups(
                    title, item_year=year, item_type='Movie', imdb_id='tt27489557',
                    require_exact=True, include_base64=False)
                self.assertTrue(result['review_needed'])
                self.assertIsNone(result['best_group'])

    def test_picker_still_offers_an_id_match_for_another_title(self):
        self.use_driver({IMDB_URL: (ITEM_URL, ITEM_PAGE)})
        result = scraper.search_tpdb_for_poster_groups(
            'Some Other Film', item_year=2024, item_type='Movie', imdb_id='tt27489557', include_base64=False)
        self.assertEqual(result['best_group']['url'], ITEM_URL)

    def bachelor_party_pages(self):
        # TPDb links TMDb collection 1707799 to an empty page; an older duplicate has the posters.
        id_url = ADVANCED + '?category=Collections&tmdb_id=1707799'
        text_url = BASE + '/search?term=Bachelor+Party+Collection&section=collections'
        empty_url, old_url = BASE + '/posters/4094767', BASE + '/posters/39146'
        card = ('<a class="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative" '
                'href="{}">Bachelor Party Collection</a>')
        pages = {
            id_url: (empty_url, '<html><head><title>Bachelor Party Collection | The Poster Database (TPDb)</title></head>'
                                '<body><p>Either no posters have been made for this item, or they\'ve all been filtered out.</p></body></html>'),
            text_url: (text_url, '<html><head><title>Search</title></head><body>'
                                 + card.format('/posters/39146') + card.format('/posters/4094767') + '</body></html>'),
            old_url: (old_url, ITEM_PAGE),
        }
        return pages, id_url, text_url, old_url

    def test_id_page_without_posters_falls_through_to_text_search(self):
        pages, id_url, text_url, old_url = self.bachelor_party_pages()
        driver = self.use_driver(pages)
        result = scraper.search_tpdb_for_poster_groups(
            'Bachelor Party Collection', item_type='Collection', tmdb_id='1707799', include_base64=False)
        self.assertEqual(driver.visited, [id_url, text_url, old_url])
        self.assertEqual(result['best_group']['url'], old_url)

    def test_id_page_without_posters_and_duplicate_pages_need_review_in_batch_mode(self):
        pages, id_url, text_url, _ = self.bachelor_party_pages()
        driver = self.use_driver(pages)
        result = scraper.search_tpdb_for_poster_groups(
            'Bachelor Party Collection', item_type='Collection', tmdb_id='1707799',
            require_exact=True, include_base64=False)
        self.assertEqual(driver.visited, [id_url, text_url])
        self.assertTrue(result['review_needed'])

    def test_wrong_collection_id_falls_back_to_the_jellyfin_title_in_batch_mode(self):
        # Jellyfin identified "[REC] Collection" as TMDb collection 140758 (the Decoys collection).
        id_url = ADVANCED + '?category=Collections&tmdb_id=140758'
        text_url = BASE + '/search?term=%5BREC%5D+Collection&section=collections'
        rec_url = BASE + '/posters/222'
        driver = self.use_driver({
            # The wrong page has posters of its own, so without the guard they would be uploaded.
            id_url: (BASE + '/posters/111', ITEM_PAGE.replace('<title> Abigail (2024) |', '<title> Decoys Collection |', 1)),
            text_url: (text_url, TEXT_RESULTS.replace('/posters/1842083', '/posters/222').replace('Abigail (2024)', '[REC] Collection')),
            rec_url: (rec_url, ITEM_PAGE),
        })
        with patch.object(scraper, '_resolve_tpdb_search_query',
                          side_effect=lambda title, **kwargs: 'Decoys Collection' if kwargs.get('tmdb_id') else title):
            result = scraper.search_tpdb_for_poster_groups(
                '[REC] Collection', item_year=2007, item_type='Collection', tmdb_id='140758',
                require_exact=True, include_base64=False)
        self.assertEqual(driver.visited, [id_url, text_url, rec_url])
        self.assertEqual(result['best_group']['url'], rec_url)


class IdLookupTests(TPDbIdSearchCase):
    def test_parses_item_redirect_fixture(self):
        self.use_driver({IMDB_URL: (ITEM_URL, ITEM_PAGE)})
        candidates = scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['title'], 'Abigail (2024)')
        self.assertEqual(candidates[0]['year'], '2024')
        self.assertEqual(candidates[0]['url'], ITEM_URL)
        self.assertEqual(candidates[0]['score'], 1.0)

    def test_redirect_title_is_unescaped_and_keeps_its_own_pipes(self):
        for page_title, expected in (
            ('Lilo &amp; Stitch (2025)', 'Lilo & Stitch (2025)'),
            ('Taylor Swift | The Eras Tour | The Final Show (2025)', 'Taylor Swift | The Eras Tour | The Final Show (2025)'),
        ):
            with self.subTest(page_title=page_title):
                page = ITEM_PAGE.replace('<title> Abigail (2024) |', f'<title> {page_title} |')
                self.use_driver({IMDB_URL: (ITEM_URL, page)})
                self.assertEqual(scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557')[0]['title'], expected)

    def test_parses_results_list_fixture(self):
        self.use_driver({IMDB_URL: (IMDB_URL, RESULTS_LIST.replace('rel="next"', 'rel="nofollow"'))})
        candidates = scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557')
        self.assertEqual(len(candidates), 36)
        self.assertEqual(candidates[0]['title'], 'The Grudge (2019)')
        self.assertEqual(candidates[0]['url'], BASE + '/posters/24442')

    def test_no_results_fixture_is_a_miss(self):
        self.use_driver({IMDB_URL: (IMDB_URL, NO_RESULTS)})
        self.assertEqual(scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557'), [])
        self.assertTrue(scraper.tpdb_advanced_search_available)

    def test_pagination_follows_rel_next_and_stops_at_cap(self):
        first = RESULTS_LIST.replace(
            f'{ADVANCED}?category=Movies&amp;year_filter=between&amp;yone=2019&amp;ytwo=2019&amp;section=movies&amp;page=2',
            f'{ADVANCED}?category=Movies&amp;imdb_id=tt1&amp;page=2')
        pages = {IMDB_URL: (IMDB_URL, first)}
        for page in range(2, 8):
            url = f'{ADVANCED}?category=Movies&imdb_id=tt1&page={page}'
            pages[url] = (url, results_page(page))
        driver = self.use_driver(pages)
        candidates = scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557')
        self.assertEqual(len(driver.visited), scraper.TPDB_ADVANCED_MAX_PAGES)
        self.assertEqual(driver.visited[-1], f'{ADVANCED}?category=Movies&imdb_id=tt1&page=5')
        self.assertEqual(len(candidates), 36 + 4)
        self.assertEqual(candidates[-1]['title'], 'Result 5 (2019)')
        self.assertEqual(scraper.time.sleep.call_count, scraper.TPDB_ADVANCED_MAX_PAGES - 1)

    def test_tmdb_timeout_is_a_miss_and_restores_page_load_timeout(self):
        driver = self.use_driver({TMDB_URL: TimeoutException('renderer timeout')})
        self.assertEqual(scraper._find_tpdb_items_by_id('Movie', tmdb_id='1111873'), [])
        self.assertEqual(driver.page_load_timeouts, [scraper.TPDB_ADVANCED_PAGE_LOAD_TIMEOUT_SEC, 30])
        self.assertTrue(scraper.tpdb_advanced_search_available)

    def test_account_without_advanced_search_falls_back_for_the_session(self):
        driver = self.use_driver({IMDB_URL: (IMDB_URL, '<html><head><title>Not Found</title></head></html>')})
        self.assertEqual(scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557', tmdb_id='1111873'), [])
        self.assertFalse(scraper.tpdb_advanced_search_available)
        self.assertEqual(scraper._find_tpdb_items_by_id('Movie', imdb_id='tt27489557'), [])
        self.assertEqual(driver.visited, [IMDB_URL])

    def test_malformed_ids_are_not_looked_up(self):
        driver = self.use_driver({})
        self.assertEqual(scraper._find_tpdb_items_by_id('Movie', imdb_id='tt0000001x', tmdb_id='abc'), [])
        self.assertEqual(driver.visited, [])

    def test_collections_use_collections_category(self):
        url = ADVANCED + '?category=Collections&tmdb_id=10'
        self.use_driver({url: (BASE + '/posters/153', '<html><head><title>Star Wars Collection | The Poster Database (TPDb)</title></head></html>')})
        candidates = scraper._find_tpdb_items_by_id('Collection', tmdb_id='10')
        self.assertEqual(candidates[0]['title'], 'Star Wars Collection')
        self.assertIsNone(candidates[0]['year'])


class IdSearchApiTests(OfflineCase):
    ITEM = {'id': 'movie', 'title': 'Abigail', 'type': 'Movie', 'year': 2024,
            'ProviderIds': {'Imdb': 'tt27489557', 'Tmdb': '1111873'}}

    def test_posters_endpoint_passes_ids_and_ignores_pre_id_cache(self):
        old_key = PickerCache(os.path.join(self.temp.name, 'picker-v2')).key({
            'item': self.ITEM, 'seasons': [], 'mapping': '', 'limit': 3, 'set': '', 'preferred_uploaders': [],
        })
        PickerCache(os.path.join(self.temp.name, 'picker-v2')).put(old_key, {'posters': ['stale text-search result']})
        result = {'posters': [], 'groups': [], 'best_group': None, 'search_query': 'Abigail'}
        with patch.object(scraper, 'get_jellyfin_items', return_value=[self.ITEM]), \
                patch.object(scraper, 'search_tpdb_for_poster_groups', return_value=result) as search:
            response = self.client.get('/item/movie/posters')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['from_cache'])
        self.assertEqual(search.call_args.kwargs['imdb_id'], 'tt27489557')
        self.assertEqual(search.call_args.kwargs['tmdb_id'], '1111873')


if __name__ == '__main__':
    unittest.main()
