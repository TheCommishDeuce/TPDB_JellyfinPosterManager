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
        return result, opened

    def test_preferred_uploader_on_page_two_is_picked_first(self):
        pages = {
            ITEM_URL: _page(1, ['Alpha', 'Bravo']),
            f'{ITEM_URL}?page=2': _page(2, ['Charlie', 'Delta'], last=True),
        }
        result, opened = self.search(pages, preferred=['delta'])
        self.assertEqual(opened, [ITEM_URL, f'{ITEM_URL}?page=2'])
        self.assertEqual(result['best_group']['show_posters'][0]['url'], 'https://theposterdb.com/api/assets/21')
        self.assertEqual(len(result['best_group']['available_sets']), 4)

    def test_stops_after_three_pages(self):
        pages = {ITEM_URL: _page(1, ['Alpha'])}
        for number in range(2, 6):
            pages[f'{ITEM_URL}?page={number}'] = _page(number, ['Alpha'])
        result, opened = self.search(pages)
        self.assertEqual(opened, [ITEM_URL, f'{ITEM_URL}?page=2', f'{ITEM_URL}?page=3'])
        self.assertEqual(len(result['best_group']['show_posters']), 3)

    def test_single_page_loads_once(self):
        result, opened = self.search({ITEM_URL: _page(1, ['Alpha'], last=True)})
        self.assertEqual(opened, [ITEM_URL])
        self.assertEqual(len(result['best_group']['show_posters']), 1)


if __name__ == '__main__':
    unittest.main()
