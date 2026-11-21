import unittest

from state_store import summarize_errors
from tests.test_workflows import ITEM, OfflineCase

MISSING = 'No matching season poster in the chosen set. Manual selection required.'


def season(number, error=MISSING):
    return {'target_id': f's{number}', 'title': f'Season {number}', 'error': error}


class SummarizeErrorsTests(unittest.TestCase):
    def test_repeated_errors_are_listed_once_with_season_ranges(self):
        targets = [season(number) for number in (1, 2, 3, 5, 7, 8)] + [
            {'target_id': 'sp', 'title': 'Specials', 'error': MISSING},
            {'target_id': 'show', 'title': 'Primary', 'error': None},
        ]
        self.assertEqual(summarize_errors(targets), f'{MISSING} (Seasons 1–3, 5, 7–8, Specials)')

    def test_distinct_errors_each_name_their_targets(self):
        targets = [season(4), season(6, 'Jellyfin rejected the upload'),
                   {'target_id': 'show', 'title': 'Primary', 'error': 'No unambiguous primary poster match.'}]
        self.assertEqual(summarize_errors(targets),
                         f'{MISSING} (Season 4); Jellyfin rejected the upload (Season 6); No unambiguous primary poster match.')

    def test_no_errors(self):
        self.assertIsNone(summarize_errors([season(1, None)]))


class FailedItemMessageTests(OfflineCase):
    def test_failed_items_list_seasons_once(self):
        for number in (1, 2, 3):
            self.store.record_target(ITEM, {'target_id': f's{number}', 'title': f'Season {number}', 'url': None,
                                            'status': 'review', 'error': MISSING}, 'auto')
        self.assertEqual(self.store.failed_items()[0]['error'], f'{MISSING} (Seasons 1–3)')


if __name__ == '__main__':
    unittest.main()
