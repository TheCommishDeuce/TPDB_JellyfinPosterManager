import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import poster_scraper as scraper
from poster_service import normalize_preferred_uploaders
from tests.test_workflows import ITEM, SELECTION, OfflineCase


def _cards(*uploaders):
    html = ''.join(
        f'''<div class="col"><div class="overlay" data-poster-id="{index}" data-poster-type="Show"></div>
            <a class="bg-transparent border-0 text-white" href="/api/assets/{index}">poster</a>
            <a href="/set/{index}">set</a><p class="uploaded-by"><a href="/user/{name}">{name}</a></p></div>'''
        for index, name in enumerate(uploaders)
    )
    return BeautifulSoup(html, 'html.parser').select(scraper.ITEM_POSTER_SELECTOR)


def _uploaders(links):
    return [scraper._extract_tpdb_card_metadata(link)['uploader'] for link in links]


class OrderingTests(unittest.TestCase):
    def test_preferred_uploaders_come_first_in_rank_order(self):
        ordered = scraper._order_by_preferred_uploaders(_cards('Alpha', 'Bravo', 'Charlie', 'Delta'), ['charlie', 'Alpha'])
        self.assertEqual(_uploaders(ordered), ['Charlie', 'Alpha', 'Bravo', 'Delta'])

    def test_without_preferences_or_matches_order_is_unchanged(self):
        cards = _cards('Alpha', 'Bravo')
        self.assertEqual(_uploaders(scraper._order_by_preferred_uploaders(cards, [])), ['Alpha', 'Bravo'])
        self.assertEqual(_uploaders(scraper._order_by_preferred_uploaders(cards, ['Zulu'])), ['Alpha', 'Bravo'])

    def test_normalization(self):
        self.assertEqual(normalize_preferred_uploaders(' @Alpha \nalpha\n\nBravo '), ['Alpha', 'Bravo'])
        for bad in ('x' * 65, ['ok', 3], {'a': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_preferred_uploaders(bad)


class PreferredUploaderApiTests(OfflineCase):
    def test_round_trip_and_validation(self):
        self.assertEqual(self.client.get('/settings/preferred-uploaders').get_json()['uploaders'], [])
        response = self.client.put('/settings/preferred-uploaders', json={'uploaders': ['Bravo', ' alpha', 'BRAVO']})
        self.assertEqual(response.get_json()['uploaders'], ['Bravo', 'alpha'])
        self.assertEqual(self.client.get('/settings/preferred-uploaders').get_json()['uploaders'], ['Bravo', 'alpha'])
        self.assertEqual(self.client.put('/settings/preferred-uploaders', json={'uploaders': 'nope' * 20}).status_code, 400)
        self.client.put('/settings/preferred-uploaders', json={'uploaders': []})
        self.assertEqual(self.client.get('/settings/preferred-uploaders').get_json()['uploaders'], [])

    def test_auto_discovery_passes_preferences_and_job_snapshot_wins(self):
        self.service.set_preferred_uploaders(['Alpha'])
        group = {'show_posters': [{'url': SELECTION['series_poster_url']}]}
        with patch('poster_service.scraper.search_tpdb_for_poster_groups', return_value={'best_group': group}) as search:
            self.service.discover(ITEM, {})
            self.assertEqual(search.call_args.kwargs['preferred_uploaders'], ['Alpha'])
            self.service.discover(ITEM, {'preferred_uploaders': ['Bravo']})
            self.assertEqual(search.call_args.kwargs['preferred_uploaders'], ['Bravo'])


if __name__ == '__main__':
    unittest.main()
