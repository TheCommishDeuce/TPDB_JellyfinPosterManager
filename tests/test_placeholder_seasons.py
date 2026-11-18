import unittest
from unittest.mock import Mock, patch

import poster_scraper as scraper
from tests.test_workflows import ITEM, SELECTION, OfflineCase

SET_URL = 'https://theposterdb.com/set/9'


def _card(poster_id, label, poster_type='Season'):
    return f'''<div class="col"><div class="hovereffect"><div class="inner">
        <div class="overlay" data-poster-id="{poster_id}" data-poster-type="{poster_type}"></div>
        <a class="bg-transparent border-0 text-white" href="/api/assets/{poster_id}" title="Example {label}">x</a>
        <a href="/set/9">set</a><p class="uploaded-by"><a href="/user/a">Alpha</a></p></div></div></div>'''


ITEM_PAGE = ''.join([
    _card(1, '(2020)', 'Show'),
    _card(2, 'Season 1'),
    _card(3, 'Specials'),
    _card(4, 'Season 3'),
])


def _poster(poster_id):
    return f'https://theposterdb.com/api/assets/{poster_id}'


class ScraperKeepsUnmatchedSeasonsTests(unittest.TestCase):
    def test_set_posters_for_missing_seasons_are_returned(self):
        driver = Mock(current_url='https://theposterdb.com/posters/123', page_source=ITEM_PAGE)
        seasons = [{'id': 's1', 'title': 'Season 1', 'number': 1, 'is_special': False}]
        with patch.object(scraper, 'selenium_driver', driver), \
                patch.object(scraper, '_open_tpdb_page_with_delay'), \
                patch.object(scraper, '_wait_for_item_posters_ready'):
            result = scraper.search_tpdb_for_poster_groups(
                'Example', item_year=2020, item_type='Series', eligible_seasons=seasons, max_posters=1,
                include_base64=False, tpdb_item_url='https://theposterdb.com/posters/123')
        group = result['best_group']
        self.assertEqual([poster['url'] for poster in group['season_posters']], [_poster(2)])
        self.assertEqual(sorted((entry['season_key'], entry['url'], entry['set_id']) for entry in group['unmatched_season_posters']),
                         [('3', _poster(4), '9'), ('specials', _poster(3), '9')])


class PlaceholderWorkflowTests(OfflineCase):
    def _group(self):
        return {'show_posters': [{'url': SELECTION['series_poster_url'], 'set_id': '9'}], 'season_posters': [],
                'unmatched_season_posters': [
                    {'season_key': 'specials', 'url': _poster(3), 'set_id': '9'},
                    {'season_key': '3', 'url': _poster(4), 'set_id': '9'},
                    {'season_key': '4', 'url': _poster(5), 'set_id': 'other-set'},
                    {'season_key': '../x', 'url': _poster(6), 'set_id': '9'},
                    {'season_key': '5', 'url': 'https://evil.test/p.jpg', 'set_id': '9'},
                ]}

    def _discover(self, seasons, options):
        with patch('poster_service.scraper.get_jellyfin_seasons', return_value=seasons), \
                patch('poster_service.scraper.search_tpdb_for_poster_groups', return_value={'best_group': self._group()}):
            return self.service.discover(ITEM, options)

    def test_saved_only_when_enabled_and_from_the_chosen_set(self):
        season1 = [{'id': 's1', 'title': 'Season 1', 'number': 1, 'is_special': False}]
        self._discover(season1, {'include_season_posters': True})
        self.assertEqual(self.service.pending_seasons(), [])
        self._discover(season1, {'include_season_posters': True, 'save_placeholder_seasons': True})
        pending = {entry['season_key']: entry['url'] for entry in self.service.pending_seasons()}
        self.assertEqual(pending, {'specials': _poster(3), '3': _poster(4)})

    def test_placeholder_applies_when_season_appears_and_is_cleared_after_upload(self):
        self.service.save_pending_seasons(ITEM, [{'season_key': 'specials', 'url': _poster(3), 'set_id': '9'}], '9')
        job = self.queue.submit('auto', options={'filter': 'pending-seasons'})
        specials = [{'id': 's0', 'title': 'Specials', 'number': 0, 'is_special': True, 'has_poster': False}]
        with patch('jobs.scraper.get_jellyfin_items', return_value=[ITEM, dict(ITEM, id='other')]), \
                patch('poster_service.scraper.get_jellyfin_seasons', return_value=specials), \
                patch('poster_service.scraper.search_tpdb_for_poster_groups') as search, \
                patch.object(self.service, 'upload', return_value=True) as upload:
            self.queue.run(job['job_id'])
        search.assert_not_called()
        self.assertEqual(self.store.get_job(job['job_id'])['total_items'], 1)
        self.assertEqual([(call.args[0]['target_id'], call.args[0]['url']) for call in upload.call_args_list], [('s0', _poster(3))])
        self.assertEqual(self.service.pending_seasons(), [])

    def test_failed_upload_keeps_placeholder_and_absent_season_is_skipped(self):
        self.service.save_pending_seasons(ITEM, [{'season_key': 'specials', 'url': _poster(3), 'set_id': '9'}], '9')
        specials = [{'id': 's0', 'title': 'Specials', 'number': 0, 'is_special': True, 'has_poster': False}]
        with patch('poster_service.scraper.get_jellyfin_seasons', return_value=specials):
            targets = self.service.discover(ITEM, {'filter': 'pending-seasons'})
        with patch('poster_service.scraper.get_jellyfin_seasons', return_value=[]):
            absent = self.service.discover(ITEM, {'filter': 'pending-seasons'})
        self.assertEqual(targets[0]['pending_key'], f"{ITEM['id']}:specials")
        self.assertEqual(absent[0]['status'], 'skipped')
        job = self.queue.submit('auto', options={'filter': 'pending-seasons'})
        with patch('jobs.scraper.get_jellyfin_items', return_value=[ITEM]), \
                patch('poster_service.scraper.get_jellyfin_seasons', return_value=specials), \
                patch.object(self.service, 'upload', return_value=False):
            self.queue.run(job['job_id'])
        self.assertEqual(len(self.service.pending_seasons()), 1)

    def test_normal_discovery_prefers_saved_placeholder_for_existing_season(self):
        self.service.save_pending_seasons(ITEM, [{'season_key': '3', 'url': _poster(4), 'set_id': '9'}], '9')
        season3 = [{'id': 's3', 'title': 'Season 3', 'number': 3, 'is_special': False, 'has_poster': False}]
        targets = self._discover(season3, {'include_season_posters': True})
        self.assertEqual([(target['target_id'], target['url'], target['status']) for target in targets],
                         [(ITEM['id'], SELECTION['series_poster_url'], 'pending'), ('s3', _poster(4), 'pending')])

    def test_api_lists_and_deletes(self):
        self.service.save_pending_seasons(ITEM, [{'season_key': 'specials', 'url': _poster(3), 'set_id': '9'},
                                                 {'season_key': '3', 'url': _poster(4), 'set_id': '9'}], '9')
        self.assertEqual(len(self.client.get('/pending-seasons').get_json()['pending']), 2)
        self.assertEqual(self.client.delete(f"/pending-seasons?key={ITEM['id']}:3").get_json()['removed'], 1)
        self.assertEqual(self.client.delete('/pending-seasons?key=nope').status_code, 404)
        self.assertEqual(self.client.delete('/pending-seasons').get_json()['removed'], 1)
        self.assertEqual(self.client.post('/jobs', json={'kind': 'auto', 'filter': 'pending-seasons'}).status_code, 202)


if __name__ == '__main__':
    unittest.main()
