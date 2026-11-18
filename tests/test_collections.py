import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import poster_scraper as scraper
from tests.test_workflows import ITEM, SELECTION, OfflineCase


def _json_response(body):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


class CollectionLibraryTests(unittest.TestCase):
    def _fetch(self, libraries, items_by_parent, item_type=None):
        seen_types = {}

        def get(url, **_kwargs):
            query = parse_qs(urlsplit(url).query)
            parent = query['ParentId'][0]
            seen_types[parent] = query['IncludeItemTypes'][0]
            return _json_response({'Items': items_by_parent.get(parent, []), 'TotalRecordCount': len(items_by_parent.get(parent, []))})

        with patch.object(scraper.requests, 'get', side_effect=get), \
                patch.object(scraper.Config, 'JELLYFIN_URL', 'http://jellyfin.test'), \
                patch.object(scraper.Config, 'JELLYFIN_API_KEY', 'key'):
            return scraper.get_jellyfin_items(item_type=item_type, libraries=libraries), seen_types

    def test_grouped_boxsets_are_collections_not_series(self):
        items, seen = self._fetch([{'id': 'movies', 'name': 'Movies', 'type': 'movies'}], {'movies': [
            {'Id': 'm1', 'Name': 'Alien', 'Type': 'Movie'},
            {'Id': 'b1', 'Name': 'Alien Collection', 'Type': 'BoxSet', 'ProviderIds': {'Tmdb': '8091'}},
        ]})
        self.assertEqual({item['id']: item['type'] for item in items}, {'m1': 'Movie', 'b1': 'Collection'})
        self.assertIsNone(next(item for item in items if item['id'] == 'b1')['season_count'])
        self.assertEqual(seen['movies'], 'Movie,Series,BoxSet')

    def test_collections_library_lists_only_boxsets_without_duplicates(self):
        libraries = [{'id': 'movies', 'name': 'Movies', 'type': 'movies'},
                     {'id': 'boxsets', 'name': 'Collections', 'type': 'boxsets'}]
        boxset = {'Id': 'b1', 'Name': 'Alien Collection', 'Type': 'BoxSet'}
        items, seen = self._fetch(libraries, {'movies': [boxset], 'boxsets': [boxset]})
        self.assertEqual([item['id'] for item in items], ['b1'])
        self.assertEqual(seen['boxsets'], 'BoxSet')
        _, seen = self._fetch(libraries, {}, item_type='movies')
        self.assertNotIn('boxsets', seen)

    def test_collection_libraries_are_opt_in(self):
        virtual_folders = [{'ItemId': 'm', 'Name': 'Movies', 'CollectionType': 'movies'},
                           {'ItemId': 'c', 'Name': 'Collections', 'CollectionType': 'boxsets'}]
        with patch.object(scraper.requests, 'get', return_value=_json_response(virtual_folders)), \
                patch.object(scraper.Config, 'JELLYFIN_URL', 'http://jellyfin.test'), \
                patch.object(scraper.Config, 'JELLYFIN_API_KEY', 'key'):
            with patch.object(scraper.Config, 'INCLUDE_COLLECTIONS', False, create=True):
                self.assertEqual([lib['id'] for lib in scraper.get_jellyfin_libraries()], ['m'])
            with patch.object(scraper.Config, 'INCLUDE_COLLECTIONS', True, create=True):
                libraries = scraper.get_jellyfin_libraries()
        self.assertEqual([(lib['id'], lib['type']) for lib in libraries], [('m', 'movies'), ('c', 'boxsets')])


class CollectionSearchTests(unittest.TestCase):
    def test_collections_search_the_collections_section(self):
        self.assertTrue(scraper._build_tpdb_search_url('Alien', item_type='Collection').endswith('&section=collections'))

    def test_tmdb_collection_lookup_uses_name_without_year(self):
        with patch.object(scraper.requests, 'get', return_value=_json_response({'name': 'Alien Collection'})) as get:
            query = scraper._resolve_tpdb_search_query('Alien Saga', item_type='Collection', tmdb_id='8091')
        self.assertEqual(query, 'Alien Collection')
        self.assertIn('/3/collection/8091', get.call_args.args[0])

    def test_collection_titles_compare_without_suffix(self):
        self.assertEqual(scraper._comparison_title('The Alien Collection', True), scraper._comparison_title('The Alien', True))
        self.assertNotEqual(scraper._comparison_title('The Alien Collection'), scraper._comparison_title('The Alien'))


class TitleFallbackTests(unittest.TestCase):
    """Issue #3: a TMDB-derived title that finds nothing retries with the Jellyfin title."""

    def test_retries_with_jellyfin_title_when_tmdb_title_finds_nothing(self):
        empty = {'groups': [], 'best_group': None, 'search_query': 'Andor (2022)'}
        found = {'groups': [{}], 'best_group': {'url': 'x'}, 'search_query': 'Andor'}
        with patch.object(scraper, '_search_tpdb_for_poster_groups', side_effect=[empty, found]) as search:
            result = scraper.search_tpdb_for_poster_groups('Andor', item_type='Series', tmdb_id='83867')
        self.assertIs(result, found)
        self.assertIsNone(search.call_args_list[1].kwargs['tmdb_id'])

    def test_no_retry_for_saved_page_same_title_or_success(self):
        cases = [
            ({'best_group': None, 'search_query': 'X (2020)'}, {'tmdb_id': '1', 'tpdb_item_url': 'https://theposterdb.com/posters/1'}),
            ({'best_group': None, 'search_query': 'Andor'}, {'tmdb_id': '1'}),
            ({'best_group': {'url': 'x'}, 'search_query': 'Andor (2022)'}, {'tmdb_id': '1'}),
            ({'best_group': None, 'search_query': 'Andor'}, {}),
        ]
        for result, kwargs in cases:
            with self.subTest(kwargs=kwargs), patch.object(scraper, '_search_tpdb_for_poster_groups', return_value=result) as search:
                self.assertIs(scraper.search_tpdb_for_poster_groups('Andor', **kwargs), result)
                self.assertEqual(search.call_count, 1)


class CollectionJobTests(OfflineCase):
    def test_collections_auto_filter(self):
        collection = dict(ITEM, id='boxset', type='Collection')
        job = self.queue.submit('auto', options={'filter': 'collections'})
        targets = [{'target_id': 'boxset', 'title': 'Primary', 'url': SELECTION['series_poster_url'], 'status': 'pending'}]
        with patch('jobs.scraper.get_jellyfin_items', return_value=[ITEM, collection]), \
                patch.object(self.service, 'discover', return_value=targets) as discover, \
                patch.object(self.service, 'upload', return_value=True):
            self.queue.run(job['job_id'])
        self.assertEqual([call.args[0]['id'] for call in discover.call_args_list], ['boxset'])
        self.assertEqual(self.client.post('/jobs', json={'kind': 'auto', 'filter': 'collections'}).status_code, 202)


if __name__ == '__main__':
    unittest.main()
