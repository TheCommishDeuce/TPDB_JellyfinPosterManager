import unittest
from unittest.mock import Mock, patch

import poster_scraper as scraper

ITEM_URL = 'https://theposterdb.com/posters/1574'


def _page(number, uploaders, last=False):
    cards = ''.join(
        f'''<div class="col"><div class="overlay" data-poster-id="{number}{index}" data-poster-type="Collection"></div>
            <a class="bg-transparent border-0 text-white" href="/api/assets/{number}{index}" title="Example Collection">x</a>
            <a href="/set/{number}{index}">set</a><p class="uploaded-by"><a href="/user/{name}">{name}</a></p></div>'''
        for index, name in enumerate(uploaders)
    )
    pager = '' if last else f'<a class="page-link" href="{ITEM_URL}?page={number + 1}" rel="next">Next</a>'
    return cards + pager


def _pages(*uploaders_per_page):
    """One page per argument, linked by next links; the last page has none."""
    pages = {}
    for number, uploaders in enumerate(uploaders_per_page, start=1):
        url = ITEM_URL if number == 1 else f'{ITEM_URL}?page={number}'
        pages[url] = _page(number, uploaders, last=number == len(uploaders_per_page))
    return pages


def _asset(page, index):
    return f'https://theposterdb.com/api/assets/{page}{index}'


class ItemPageTests(unittest.TestCase):
    def search(self, pages, preferred=None):
        driver = Mock(current_url=ITEM_URL, page_source=pages[ITEM_URL])
        opened = []

        def open_page(url):
            opened.append(url)
            driver.page_source = pages[url]

        with patch.object(scraper, 'selenium_driver', driver), \
                patch.object(scraper, '_open_tpdb_page_with_delay', side_effect=open_page), \
                patch.object(scraper, '_wait_for_item_posters_ready'):
            result = scraper.search_tpdb_for_poster_groups(
                'Example Collection', item_type='Collection', include_base64=False,
                tpdb_item_url=ITEM_URL, preferred_uploaders=preferred)
        return [poster['url'] for poster in result['best_group']['show_posters']], opened

    def test_preferred_uploader_on_the_last_page_is_picked_first(self):
        posters, opened = self.search(_pages(['Alpha'], ['Bravo'], ['Bravo'], ['Bravo'], ['Delta']), preferred=['delta'])
        self.assertEqual(len(opened), 5)
        self.assertEqual(posters[0], _asset(5, 0))

    def test_later_pages_keep_only_preferred_uploaders(self):
        posters, _ = self.search(_pages(['Alpha', 'Bravo'], ['Charlie', 'Delta']), preferred=['Delta'])
        self.assertEqual(posters, [_asset(2, 1), _asset(1, 0), _asset(1, 1)])

    def test_without_preferred_uploaders_only_page_one_is_loaded(self):
        posters, opened = self.search(_pages(['Alpha'], ['Bravo']))
        self.assertEqual(opened, [ITEM_URL])
        self.assertEqual(posters, [_asset(1, 0)])

    def test_stops_at_the_page_cap(self):
        with patch.object(scraper, 'TPDB_ITEM_MAX_PAGES', 3):
            _, opened = self.search(_pages(*[['Alpha']] * 5), preferred=['Alpha'])
        self.assertEqual(opened, [ITEM_URL, f'{ITEM_URL}?page=2', f'{ITEM_URL}?page=3'])

    def test_single_page_loads_once(self):
        posters, opened = self.search(_pages(['Alpha']), preferred=['Alpha'])
        self.assertEqual(opened, [ITEM_URL])
        self.assertEqual(posters, [_asset(1, 0)])


if __name__ == '__main__':
    unittest.main()
