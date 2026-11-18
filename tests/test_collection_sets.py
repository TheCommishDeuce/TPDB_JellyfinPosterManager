import unittest
from unittest.mock import patch

import poster_scraper as scraper
from tests.test_workflows import OfflineCase

COLLECTION = {'id': 'boxset', 'title': 'Ice Age Collection', 'type': 'Collection', 'year': None}


def _card(poster_id, poster_type, title):
    # Mirrors TPDb set-page markup (logged-in download anchor + typed overlay).
    return f'''<div class="col-6 col-lg-2 p-1"><div class="hovereffect rounded-poster">
        <picture><source srcset="https://images.theposterdb.com/prod/public/images/posters/optimized/x/{poster_id}.webp" type="image/webp"/>
        <img class="w-100 rounded-poster tpdb-poster" src="/images/defaults/missing_poster.jpg"/></picture>
        <div class="overlay rounded-poster" data-poster-id="{poster_id}" data-poster-type="{poster_type}">
          <div class="row m-0 h-100 p-3"><div class="col-11 p-0 pr-1 poster-title-correction">
            <p class="p-0 mb-1 text-break">{title}</p>
            <p class="uploaded-by text-white d-inline-block text-truncate w-100">by <a href="https://theposterdb.com/user/DIIIVOY">DIIIVOY</a></p></div>
          <div class="col-1 p-0"><a class="badge set_poster_count" href="https://theposterdb.com/set/65009">17</a></div>
          <div class="col-12 p-0"><a class="text-white" href="https://theposterdb.com/posters/{poster_id}" title="Movie"></a>
            <a class="bg-transparent border-0 text-white" href="/api/assets/{poster_id}/download"></a></div></div>
        </div></div></div>'''


SET_PAGE = ''.join([
    _card(1, 'collection', 'Ice Age Collection'),
    _card(2, 'collection', 'Rio Collection'),
    _card(3, 'movie', 'Ice Age (2002)'),
    _card(4, 'movie', 'Ice Age: The Meltdown (2006)'),
    _card(5, 'movie', 'Rio (2011)'),
    _card(6, 'category', 'Children'),
])
CHILDREN = [
    {'id': 'm1', 'title': 'Ice Age', 'year': 2002, 'has_poster': True},
    {'id': 'm2', 'title': 'Ice Age: The Meltdown', 'year': 2006, 'has_poster': False},
    {'id': 'm3', 'title': 'Ice Age: Continental Drift', 'year': 2012, 'has_poster': True},
    {'id': 'm4', 'title': 'Ice Age', 'year': 1999, 'has_poster': True},
]


def _asset(poster_id):
    return f'https://theposterdb.com/api/assets/{poster_id}/download'


class SetParsingAndMatchingTests(unittest.TestCase):
    def test_set_page_cards_are_typed(self):
        posters = scraper.parse_tpdb_set_posters(SET_PAGE)
        self.assertEqual([(p['poster_type'], p['title'], p['year']) for p in posters], [
            ('collection', 'Ice Age Collection', None), ('collection', 'Rio Collection', None),
            ('movie', 'Ice Age (2002)', '2002'), ('movie', 'Ice Age: The Meltdown (2006)', '2006'),
            ('movie', 'Rio (2011)', '2011'), ('category', 'Children', None)])
        self.assertEqual(posters[2]['url'], _asset(3))
        self.assertEqual(posters[2]['set_id'], '65009')

    def test_movies_match_on_exact_title_and_year(self):
        result = scraper.match_collection_set('Ice Age Collection', CHILDREN, scraper.parse_tpdb_set_posters(SET_PAGE))
        self.assertEqual(result['collection_poster']['url'], _asset(1))
        self.assertEqual([(m['child']['id'], m['poster']['url']) for m in result['matches']], [('m1', _asset(3)), ('m2', _asset(4))])
        self.assertEqual([child['id'] for child in result['unmatched_children']], ['m3', 'm4'])
        self.assertEqual([poster['title'] for poster in result['unmatched_posters']], ['Rio (2011)'])

    def test_collection_poster_needs_title_match_unless_only_one(self):
        posters = scraper.parse_tpdb_set_posters(SET_PAGE)
        self.assertIsNone(scraper.match_collection_set('Shrek Collection', [], posters)['collection_poster'])
        single = [poster for poster in posters if poster['title'] != 'Rio Collection']
        self.assertEqual(scraper.match_collection_set('Ice Age Saga', [], single)['collection_poster']['url'], _asset(1))


class CollectionSetWorkflowTests(OfflineCase):
    def setUp(self):
        super().setUp()
        patches = [
            patch.object(scraper, 'get_jellyfin_items', return_value=[COLLECTION]),
            patch.object(scraper, 'get_jellyfin_collection_children', return_value=[dict(child) for child in CHILDREN]),
            patch.object(scraper, 'get_tpdb_set_posters', side_effect=lambda url: scraper.parse_tpdb_set_posters(SET_PAGE)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_preview_endpoint(self):
        self.store.set_protected('m2', True)
        data = self.client.get('/item/boxset/collection-set?set_url=https://theposterdb.com/set/65009').get_json()
        self.assertEqual(len(data['matches']), 2)
        self.assertEqual({m['child']['id']: m['child']['protected'] for m in data['matches']}, {'m1': False, 'm2': True})
        self.assertEqual(self.client.get('/item/boxset/collection-set?set_url=https://evil.test/set/1').status_code, 400)

    def test_selection_with_child_posters_is_validated(self):
        selection = {'series_poster_url': _asset(1), 'child_posters': {'m1': {'url': _asset(3), 'title': 'Ice Age'}}}
        self.assertEqual(self.client.post('/item/boxset/select', json={'selection': selection}).status_code, 200)
        targets = self.service.targets(COLLECTION, selection)
        self.assertEqual([(t['target_id'], t.get('child', False)) for t in targets], [('boxset', False), ('m1', True)])
        foreign = {'series_poster_url': _asset(1), 'child_posters': {'other-movie': _asset(3)}}
        self.assertEqual(self.client.post('/item/boxset/select', json={'selection': foreign}).status_code, 400)
        with self.assertRaises(ValueError):
            self.service.targets(dict(COLLECTION, type='Movie'), selection)

    def test_auto_discovery_adds_exact_matches_and_skips_protected(self):
        self.store.set_protected('m2', True)
        group = {'show_posters': [{'url': _asset(1), 'set_id': '65009', 'set_url': 'https://theposterdb.com/set/65009'}]}
        with patch('poster_service.scraper.search_tpdb_for_poster_groups', return_value={'best_group': group}):
            plain = self.service.discover(COLLECTION, {})
            targets = self.service.discover(COLLECTION, {'include_collection_movies': True})
        self.assertEqual(len(plain), 1)
        self.assertEqual([(t['target_id'], t['url'], t['status']) for t in targets],
                         [('boxset', _asset(1), 'pending'), ('m1', _asset(3), 'pending'), ('m2', _asset(4), 'skipped')])

    def test_child_protected_after_selection_is_skipped_at_upload(self):
        selection = {'series_poster_url': _asset(1), 'child_posters': {'m1': _asset(3)}}
        self.store.set_selection(COLLECTION, selection)
        self.store.set_protected('m1', True)
        job = self.queue.submit('manual', tasks=[{'item': COLLECTION, 'selection': selection}])
        with patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(job['job_id'])
        self.assertEqual([call.args[0]['target_id'] for call in upload.call_args_list], ['boxset'])
        result = self.store.get_job(job['job_id'])['results'][0]
        self.assertEqual(result['season_results'][0]['is_movie'], True)
        self.assertEqual(result['season_results'][0]['error'], 'Protected movie')


if __name__ == '__main__':
    unittest.main()
