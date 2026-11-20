import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import poster_scraper as scraper

ITEM_URL = 'https://theposterdb.com/posters/938101'
TITLE = 'A Disturbance in the Force: How the Star Wars Holiday Special Happened (2023)'


def card(poster_type, title, poster_id='451214'):
    """A TPDb poster card, trimmed from a real item page."""
    return (
        '<div class="hovereffect rounded-poster"><picture>'
        f'<source class="w-100 rounded-poster" srcset="https://images.theposterdb.com/prod/public/images/posters/optimized/movies/938101/{poster_id}.jpg" type="image/jpeg"/>'
        '<img class="w-100 rounded-poster tpdb-poster" loading="lazy" src="/images/defaults/missing_poster.jpg"/></picture>'
        f'<div class="overlay rounded-poster" data-mobile-click="false" data-poster-id="{poster_id}" data-poster-type="{poster_type}">'
        '<div class="row m-0 h-100 p-3"><div class="col-11 p-0 pr-1 poster-title-correction">'
        f'<p class="p-0 mb-1 text-break">{title}</p>'
        '<p class="uploaded-by text-white d-inline-block text-truncate w-100">by <a href="https://theposterdb.com/user/fwlolx">fwlolx</a></p></div>'
        '<div class="col-1 p-0 d-flex flex-column align-items-center">'
        '<a class="badge badge-pill badge-primary mt-2 set_poster_count" href="https://theposterdb.com/set/150245" title="Posters in Set">34</a></div>'
        '<div class="col-12 p-0 d-flex flex-row justify-content-between align-items-center">'
        f'<a class="bg-transparent border-0 text-white" download="{title}.png" '
        f'href="https://theposterdb.com/api/assets/{poster_id}/download?performed_by=fixture-user" title="Download Poster">'
        '<span class="d-none">Download</span></a></div></div></div></div>'
    )


def poster_link(html):
    return BeautifulSoup(html, 'html.parser').select_one(scraper.ITEM_POSTER_SELECTOR)


class FakeDriver:
    def __init__(self, html):
        self.html = html
        self.current_url = 'about:blank'
        self.page_source = ''

    def get(self, url):
        self.current_url, self.page_source = url, self.html

    def find_elements(self, by, selector):
        return BeautifulSoup(self.page_source, 'html.parser').select(selector)


class MovieSpecialsTests(unittest.TestCase):
    def test_movie_and_collection_cards_never_get_a_season(self):
        self.assertIsNone(scraper._extract_tpdb_season_key(poster_link(card('movie', TITLE))))
        self.assertIsNone(scraper._extract_tpdb_season_key(poster_link(card('collection', 'Holiday Specials Collection'))))
        self.assertIsNone(scraper._extract_tpdb_season_key(poster_link(card('movie', 'Ocean S01 (2001)'))))

    def test_show_season_cards_are_still_detected_from_their_title(self):
        self.assertEqual(scraper._extract_tpdb_season_key(poster_link(card('show', 'Breaking Bad (2008) - Specials'))), 'specials')
        self.assertEqual(scraper._extract_tpdb_season_key(poster_link(card('show', 'Breaking Bad (2008) - Season 2'))), '2')
        self.assertIsNone(scraper._extract_tpdb_season_key(poster_link(card('show', 'Breaking Bad (2008)'))))

    def test_movie_with_special_in_title_returns_its_posters(self):
        page = (f'<html><head><title>{TITLE} | The Poster Database (TPDb)</title></head><body>'
                + card('movie', TITLE, '451214') + card('movie', TITLE, '441715') + '</body></html>')
        with patch.object(scraper, 'selenium_driver', FakeDriver(page)), \
                patch.object(scraper.time, 'sleep'), \
                patch('socket.socket.connect', side_effect=AssertionError('Network is disabled in tests')):
            result = scraper.search_tpdb_for_poster_groups(
                'A Disturbance in the Force: How the Star Wars Holiday Special Happened', item_year=2023,
                item_type='Movie', tpdb_item_url=ITEM_URL, include_base64=False)
        self.assertEqual(len(result['posters']), 2)
        self.assertEqual(result['best_group']['unmatched_season_posters'], [])
        self.assertEqual(result['posters'][0]['uploader'], 'fwlolx')


if __name__ == '__main__':
    unittest.main()
