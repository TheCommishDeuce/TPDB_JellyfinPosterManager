"""Shared poster selection, matching and upload rules for every entry point."""
import os
import re
import tempfile
from datetime import datetime, timezone

import poster_scraper as scraper
from safe_http import ImageLoginRequired, normalize_tpdb_page, validate_image_url


class NeedsReview(ValueError):
    pass


MAX_PREFERRED_UPLOADERS = 50


def normalize_preferred_uploaders(value):
    """Validate an ordered list of TPDb usernames; trims and drops case-insensitive duplicates."""
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError('Preferred uploaders must be a list of usernames')
    names, seen = [], set()
    for name in value:
        name = name.strip().lstrip('@').strip()
        if not name or name.casefold() in seen:
            continue
        if len(name) > 64 or any(char.isspace() and char != ' ' for char in name):
            raise ValueError(f'Invalid uploader name: {name[:64]!r}')
        seen.add(name.casefold())
        names.append(name)
    if len(names) > MAX_PREFERRED_UPLOADERS:
        raise ValueError(f'At most {MAX_PREFERRED_UPLOADERS} preferred uploaders are supported')
    return names


class PosterService:
    def __init__(self, store):
        self.store = store

    def targets(self, item, selection):
        if isinstance(selection, str):
            selection = {'series_poster_url': selection}
        if not isinstance(selection, dict):
            raise ValueError('Invalid poster selection')
        primary = selection.get('series_poster_url') or selection.get('poster_url')
        seasons = selection.get('season_posters') or {}
        if not isinstance(seasons, dict) or (seasons and item.get('type') != 'Series'):
            raise ValueError('Invalid season selection')
        targets = []
        if primary:
            targets.append({'target_id': item['id'], 'title': 'Primary', 'url': primary})
        for season_id, season in seasons.items():
            if not isinstance(season_id, str) or not season_id or season_id == item['id']:
                raise ValueError('Invalid season ID')
            url = season.get('url') if isinstance(season, dict) else season
            title = season.get('title') if isinstance(season, dict) else 'Season'
            targets.append({'target_id': season_id, 'title': title or 'Season', 'url': url})
        if not targets:
            raise ValueError('Choose at least one poster')
        for target in targets:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', target['target_id']):
                raise ValueError('Invalid Jellyfin target ID')
            validate_image_url(target['url'], 'tpdb')
            target['status'] = 'pending'
        return targets

    def validate_seasons(self, item, targets):
        season_ids = {target['target_id'] for target in targets if target['target_id'] != item['id']}
        if season_ids and not season_ids.issubset({season['id'] for season in scraper.get_jellyfin_seasons(item['id'])}):
            raise ValueError('Selected seasons do not belong to this series or are not eligible')

    def preferred_uploaders(self):
        try:
            return normalize_preferred_uploaders(self.store.values('prefs').get('preferred_uploaders') or [])
        except ValueError:
            return []

    def set_preferred_uploaders(self, value):
        names = normalize_preferred_uploaders(value)
        self.store.set_value('prefs', 'preferred_uploaders', names or None)
        return names

    # Placeholder season posters (#32): TPDb set posters for seasons Jellyfin doesn't have yet.
    PENDING_NAMESPACE = 'pending_seasons'

    def pending_seasons(self, item_id=None):
        entries = []
        for key, entry in self.store.values(self.PENDING_NAMESPACE).items():
            if isinstance(entry, dict) and (item_id is None or entry.get('item_id') == item_id):
                entries.append(dict(entry, key=key))
        return entries

    def save_pending_seasons(self, item, posters, set_id):
        saved = 0
        for poster in posters:
            if set_id and poster.get('set_id') != set_id:
                continue
            season_key = str(poster.get('season_key') or '')
            if not re.fullmatch(r'specials|\d{1,3}', season_key):
                continue
            try:
                validate_image_url(poster.get('url'), 'tpdb')
            except ValueError:
                continue
            self.store.set_value(self.PENDING_NAMESPACE, f"{item['id']}:{season_key}", {
                'item_id': item['id'], 'item_title': item.get('title'), 'season_key': season_key,
                'url': poster['url'], 'set_id': poster.get('set_id'), 'set_url': poster.get('set_url'),
                'uploader': poster.get('uploader'), 'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            })
            saved += 1
        return saved

    def clear_pending_season(self, key):
        self.store.set_value(self.PENDING_NAMESPACE, key, None)

    def pending_targets(self, item, seasons, replace_existing=False):
        """Upload targets for placeholders whose season now exists in Jellyfin."""
        by_key = {scraper._season_key_from_jellyfin(season): season for season in seasons}
        targets = []
        for entry in self.pending_seasons(item['id']):
            season = by_key.get(entry['season_key'])
            if not season or (season.get('has_poster') and not replace_existing):
                continue
            validate_image_url(entry['url'], 'tpdb')
            targets.append({'target_id': season['id'], 'title': season.get('title', 'Season'), 'url': entry['url'],
                            'status': 'pending', 'pending_key': entry['key']})
        return targets

    def discover(self, item, options):
        if options.get('filter') == 'pending-seasons':
            seasons = scraper.get_jellyfin_seasons(item['id']) if item.get('type') == 'Series' else []
            targets = self.pending_targets(item, seasons, options.get('replace_existing_season_posters'))
            return targets or [{'target_id': item['id'], 'title': 'Placeholder seasons', 'url': None,
                                'status': 'skipped', 'error': 'No placeholder season has appeared in Jellyfin yet'}]
        seasons = scraper.get_jellyfin_seasons(item['id']) if options.get('include_season_posters') and item.get('type') == 'Series' else []
        mapping = normalize_tpdb_page(self.store.values('mapping').get(item['id']))
        result = scraper.search_tpdb_for_poster_groups(
            item['title'], item_year=item.get('year'), item_type=item.get('type'),
            tmdb_id=item.get('ProviderIds', {}).get('Tmdb'), eligible_seasons=seasons,
            max_posters=1, include_base64=False, tpdb_item_url=mapping, require_exact=not bool(mapping),
            preferred_uploaders=options.get('preferred_uploaders', self.preferred_uploaders()),
        )
        group = result.get('best_group')
        if not group or not group.get('show_posters'):
            raise NeedsReview('No unambiguous primary poster match. Choose a poster or correct the TPDb page.')
        primary = group['show_posters'][0]
        targets = self.targets(item, primary['url'])
        if seasons and options.get('save_placeholder_seasons'):
            self.save_pending_seasons(item, group.get('unmatched_season_posters', []), primary.get('set_id'))
        placeholders = {target['target_id']: target for target in self.pending_targets(
            item, seasons, options.get('replace_existing_season_posters'))} if seasons else {}
        for season in seasons:
            if season['id'] in placeholders:
                # A saved placeholder wins: it's the set the user chose when the season didn't exist.
                targets.append(placeholders[season['id']])
                continue
            if season.get('has_poster') and not options.get('replace_existing_season_posters'):
                continue
            poster = next((poster for poster in group.get('season_posters', [])
                           if poster.get('season_id') == season['id'] and primary.get('set_id')
                           and poster.get('set_id') == primary['set_id']), None)
            targets.append({
                'target_id': season['id'], 'title': season.get('title', 'Season'),
                'url': poster['url'] if poster else None,
                'status': 'pending' if poster else 'review',
                'error': None if poster else 'No matching season poster in the chosen set. Manual selection required.',
            })
        return targets

    def retry_tasks(self, item_ids=None):
        tasks = []
        for entry in self.store.failed_items(limit=1000000):
            if item_ids is not None and entry['item_id'] not in item_ids:
                continue
            targets = [dict(target, status='pending', error=None) for target in entry['failed_targets']
                       if target.get('url') and target['status'] == 'failed']
            if targets:
                tasks.append({'item': entry['item'], 'targets': targets})
        return tasks

    def upload(self, target):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', target['target_id']):
            raise ValueError('Invalid Jellyfin target ID')
        validate_image_url(target['url'], 'tpdb')
        scraper.setup_selenium_and_login()
        directory = getattr(scraper.Config, 'TEMP_POSTER_DIR', 'cache/temp_posters')
        os.makedirs(directory, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='poster_', suffix='.jpg', dir=directory)
        os.close(fd)
        try:
            try:
                downloaded = scraper.download_image_with_cookies(target['url'], path)
            except ImageLoginRequired:
                scraper.setup_selenium_and_login(force=True)
                downloaded = scraper.download_image_with_cookies(target['url'], path)
            if not downloaded:
                raise RuntimeError('Could not download or decode poster')
            return scraper.upload_image_to_jellyfin_improved(target['target_id'], path)
        finally:
            os.unlink(path)
