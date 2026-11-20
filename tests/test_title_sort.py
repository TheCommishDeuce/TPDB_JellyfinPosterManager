import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import poster_scraper as scraper

LIBRARY = {'id': 'movies', 'name': 'Movies', 'type': 'movies'}
JELLYFIN_ITEMS = [
    {'Id': '1', 'Name': 'The Abyss', 'SortName': 'abyss', 'ProductionYear': 1989, 'Type': 'Movie'},
    {'Id': '2', 'Name': 'Big', 'SortName': 'big', 'ProductionYear': 1988, 'Type': 'Movie'},
    {'Id': '3', 'Name': 'An American Tail', 'SortName': 'american tail', 'ProductionYear': 1986, 'Type': 'Movie'},
    {'Id': '4', 'Name': 'A Bug\'s Life', 'SortName': 'bug\'s life', 'ProductionYear': 1988, 'Type': 'Movie'},
]


class TitleSortTests(unittest.TestCase):
    def fetch(self, sort_by, items=JELLYFIN_ITEMS):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'Items': items, 'TotalRecordCount': len(items)}
        with patch.object(scraper.requests, 'get', return_value=response) as get, \
                patch.object(scraper.Config, 'JELLYFIN_URL', 'http://jellyfin.invalid'), \
                patch.object(scraper.Config, 'JELLYFIN_API_KEY', 'key'):
            result = scraper.get_jellyfin_items(sort_by=sort_by, libraries=[LIBRARY])
        self.fields = parse_qs(urlsplit(get.call_args.args[0]).query)['Fields'][0].split(',')
        return [item['title'] for item in result]

    def test_name_sort_ignores_leading_articles(self):
        self.assertEqual(self.fetch('name'), ['The Abyss', 'An American Tail', 'Big', "A Bug's Life"])
        self.assertIn('SortName', self.fields)

    def test_library_and_year_sorts_break_ties_by_sort_title(self):
        self.assertEqual(self.fetch('library'), ['The Abyss', 'An American Tail', 'Big', "A Bug's Life"])
        self.assertEqual(self.fetch('year'), ['An American Tail', 'Big', "A Bug's Life", 'The Abyss'])

    def test_missing_sort_name_falls_back_to_the_title(self):
        items = [dict(item, SortName=None) for item in JELLYFIN_ITEMS]
        self.assertEqual(self.fetch('name', items), ["A Bug's Life", 'An American Tail', 'Big', 'The Abyss'])


if __name__ == '__main__':
    unittest.main()
