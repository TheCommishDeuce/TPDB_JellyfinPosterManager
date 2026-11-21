"""Durable local state. Each mutation uses a short SQLite transaction."""
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _target_names(titles):
    """Season titles become ranges ("Seasons 1-3, 5"); other titles are kept as they are."""
    numbers = sorted({int(match.group(1)) for title in titles if (match := re.fullmatch(r'Season (\d+)', title))})
    others = [title for title in dict.fromkeys(titles) if not re.fullmatch(r'Season \d+', title)]
    ranges = []
    for number in numbers:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    spans = [f'{start}' if start == end else f'{start}\u2013{end}' for start, end in ranges]
    seasons = [f"Season{'s' if len(numbers) > 1 else ''} {', '.join(spans)}"] if spans else []
    return ', '.join(seasons + others)


def summarize_errors(targets, title_key='title', default=''):
    """Each distinct error once, followed by the targets it hit: "No poster (Seasons 1-28, 30)"."""
    grouped = {}
    for target in targets:
        error = target.get('error') or default
        if error:
            grouped.setdefault(error, []).append(target.get(title_key) or '')
    parts = []
    for error, titles in grouped.items():
        names = _target_names([title for title in titles if title and title != 'Primary'])
        parts.append(f'{error} ({names})' if names else error)
    return '; '.join(parts) or None


class StateStore:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    namespace TEXT, key TEXT, value TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                );
                CREATE TABLE IF NOT EXISTS selections (
                    item_id TEXT PRIMARY KEY, item TEXT NOT NULL, selection TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    seq INTEGER PRIMARY KEY, item_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outcome_target ON outcomes(item_id, target_id, seq);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def values(self, namespace):
        with self.connect() as db:
            return {row['key']: json.loads(row['value']) for row in db.execute(
                'SELECT key, value FROM settings WHERE namespace=? ORDER BY rowid', (namespace,))}

    def set_value(self, namespace, key, value):
        with self.connect() as db:
            if value is None:
                db.execute('DELETE FROM settings WHERE namespace=? AND key=?', (namespace, str(key)))
            else:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?, ?)',
                           (namespace, str(key), json.dumps(value)))

    def set_protected(self, item_id, protected=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            exists = db.execute("SELECT 1 FROM settings WHERE namespace='protected' AND key=?", (item_id,)).fetchone()
            protected = not bool(exists) if protected is None else protected
            if protected:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('protected', ?, 'true')", (item_id,))
            else:
                db.execute("DELETE FROM settings WHERE namespace='protected' AND key=?", (item_id,))
        return protected

    def selections(self):
        with self.connect() as db:
            return {row['item_id']: {'item': json.loads(row['item']), 'selection': json.loads(row['selection'])}
                    for row in db.execute('SELECT * FROM selections')}

    def set_selection(self, item, selection):
        with self.connect() as db:
            if selection is None:
                db.execute('DELETE FROM selections WHERE item_id=?', (item['id'],))
            else:
                db.execute('INSERT OR REPLACE INTO selections VALUES (?, ?, ?)',
                           (item['id'], json.dumps(item), json.dumps(selection, sort_keys=True)))
                db.execute("DELETE FROM settings WHERE namespace='queue' AND key=?", (item['id'],))

    def clear_selection_if_unchanged(self, item_id, selection):
        with self.connect() as db:
            db.execute('DELETE FROM selections WHERE item_id=? AND selection=?',
                       (item_id, json.dumps(selection, sort_keys=True)))

    def _insert_outcome(self, db, item, target, operation):
        entry = {
            'timestamp': target.get('timestamp') or timestamp(), 'item_id': item['id'], 'item_title': item.get('title') or item['id'],
            'item_type': item.get('type'), 'item_year': item.get('year'),
            'operation': operation, 'target_id': target['target_id'],
            'target_title': target.get('title', 'Primary'), 'poster_url': target.get('url'),
            'status': target['status'], 'error': target.get('error'), 'item': item,
        }
        db.execute('INSERT INTO outcomes(item_id, target_id, status, payload) VALUES (?, ?, ?, ?)',
                   (item['id'], target['target_id'], target['status'], json.dumps(entry)))

    def record_target(self, item, target, operation):
        with self.connect() as db:
            self._insert_outcome(db, item, target, operation)

    def latest_targets(self):
        with self.connect() as db:
            rows = db.execute('''SELECT payload FROM outcomes WHERE seq IN
                (SELECT MAX(seq) FROM outcomes GROUP BY item_id, target_id) ORDER BY seq DESC''')
            return [json.loads(row['payload']) for row in rows]

    def _summaries(self):
        grouped = {}
        for entry in self.latest_targets():
            grouped.setdefault(entry['item_id'], []).append(entry)
        summaries = []
        for item_id, entries in grouped.items():
            latest = dict(entries[0])
            failed = [entry for entry in entries if entry['status'] in ('failed', 'review')]
            successes = [entry for entry in entries if entry['status'] == 'success']
            if not failed and not successes:
                continue
            incomplete = any(entry['status'] in ('pending', 'uploading') for entry in entries)
            latest['status'] = 'failed' if failed else 'pending' if incomplete else 'success'
            latest['error'] = summarize_errors(failed, title_key='target_title', default='Upload failed')
            latest['failed_targets'] = [
                {'target_id': entry['target_id'], 'title': entry['target_title'], 'url': entry.get('poster_url'),
                 'status': entry['status'], 'error': entry.get('error')}
                for entry in failed
            ]
            latest['needs_review'] = any(not entry.get('poster_url') or entry['status'] == 'review' for entry in failed)
            latest['poster_targets'] = {
                'series_poster': any(entry['target_id'] == item_id for entry in successes),
                'season_count': sum(entry['target_id'] != item_id for entry in successes),
                'season_titles': [entry['target_title'] for entry in successes if entry['target_id'] != item_id],
            }
            latest['season_results'] = [
                {'season_id': entry['target_id'], 'season_title': entry['target_title'],
                 'success': entry['status'] == 'success', 'error': entry.get('error'), 'poster_url': entry.get('poster_url')}
                for entry in entries if entry['target_id'] != item_id
            ]
            summaries.append(latest)
        return summaries

    def failed_items(self, limit=500):
        return [entry for entry in self._summaries() if entry['status'] == 'failed'][:limit]

    def processed_items(self, limit=1000):
        return [entry for entry in self._summaries() if entry['status'] == 'success'][:limit]

    def clear_history(self, status, item_ids=None):
        if item_ids == []:
            return 0
        statuses = ('failed', 'review') if status == 'failed' else ('success',)
        sql = 'DELETE FROM outcomes WHERE status IN (' + ','.join('?' for _ in statuses) + ')'
        args = list(statuses)
        if item_ids is not None:
            sql += ' AND item_id IN (' + ','.join('?' for _ in item_ids) + ')'
            args.extend(item_ids)
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def save_job(self, job, outcome=None, planned=False):
        job['updated_at'] = timestamp()
        with self.connect() as db:
            db.execute('''INSERT INTO jobs VALUES (?, ?, ?) ON CONFLICT(id)
                          DO UPDATE SET status=excluded.status, payload=excluded.payload''',
                       (job['job_id'], job['status'], json.dumps(job)))
            if planned:
                for task in job.get('tasks', []):
                    for target in task.get('targets', []):
                        if target['status'] == 'pending':
                            previous = db.execute('''SELECT status FROM outcomes WHERE item_id=? AND target_id=?
                                                     ORDER BY seq DESC LIMIT 1''',
                                                  (task['item']['id'], target['target_id'])).fetchone()
                            # Scheduling a retry is not evidence that its failure is resolved.
                            if not previous or previous['status'] not in ('failed', 'review'):
                                self._insert_outcome(db, task['item'], target, job['kind'])
            if outcome:
                self._insert_outcome(db, *outcome)
                item, target, _ = outcome
                if target['status'] == 'success' and job['kind'] in ('manual', 'retry'):
                    self._consume_selected_target(db, item['id'], target)

    def _consume_selected_target(self, db, item_id, target):
        row = db.execute('SELECT selection FROM selections WHERE item_id=?', (item_id,)).fetchone()
        if not row:
            return
        selection = json.loads(row['selection'])
        if isinstance(selection, str):
            if target['target_id'] == item_id and target.get('url') == selection:
                db.execute('DELETE FROM selections WHERE item_id=?', (item_id,))
            return
        if target['target_id'] == item_id:
            for key in ('series_poster_url', 'poster_url'):
                if selection.get(key) == target.get('url'):
                    selection.pop(key)
        else:
            seasons = selection.get('season_posters') or {}
            season = seasons.get(target['target_id'])
            url = season.get('url') if isinstance(season, dict) else season
            if url and url == target.get('url'):
                seasons.pop(target['target_id'])
        if not selection.get('series_poster_url') and not selection.get('poster_url') and not selection.get('season_posters'):
            db.execute('DELETE FROM selections WHERE item_id=?', (item_id,))
        else:
            db.execute('UPDATE selections SET selection=? WHERE item_id=?',
                       (json.dumps(selection, sort_keys=True), item_id))

    def get_job(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
            return json.loads(row['payload']) if row else None

    def jobs(self):
        with self.connect() as db:
            return [json.loads(row['payload']) for row in db.execute('SELECT payload FROM jobs ORDER BY rowid DESC')]

    def import_legacy(self, config):
        """One transaction, one import marker. Originals are never modified."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM settings WHERE namespace='migration' AND key='legacy-v1'").fetchone():
                return

            def read_json(key, default):
                path = config.get(key)
                if not path or not os.path.exists(path):
                    return default
                with open(path, encoding='utf-8') as source:
                    return json.load(source)

            protected = read_json('PROTECTED_ITEMS_FILE', [])
            if isinstance(protected, dict):
                protected = protected.get('items', [])
            for item_id in protected:
                db.execute("INSERT OR IGNORE INTO settings VALUES ('protected', ?, 'true')", (str(item_id),))
            for item_id, url in read_json('TPDB_ITEM_MAP_FILE', {}).items():
                db.execute("INSERT OR IGNORE INTO settings VALUES ('mapping', ?, ?)", (str(item_id), json.dumps(url)))

            entries = []
            for key in ('RESULTS_LOG_FILE', 'FAILED_LOG_FILE'):
                path = config.get(key)
                if not path or not os.path.exists(path):
                    continue
                with open(path, encoding='utf-8') as source:
                    for line_number, line in enumerate(source, 1):
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            logging.warning('Legacy log %s line %s is not JSON; original retained.', path, line_number)
                            continue
                        if isinstance(entry, dict) and entry.get('item_id'):
                            entries.append(entry)
            for entry in sorted(entries, key=lambda entry: entry.get('timestamp') or ''):
                item = {'id': entry['item_id'], 'title': entry.get('item_title'),
                        'type': entry.get('item_type'), 'year': entry.get('item_year')}
                status = entry.get('status', 'failed')
                if status == 'resolved':
                    # A legacy item-level resolution cannot prove season failures were fixed.
                    continue
                primary_applied = entry.get('poster_targets', {}).get('series_poster', bool(entry.get('poster_url')))
                if status != 'success' or primary_applied or not entry.get('season_results'):
                    target = {'target_id': item['id'], 'title': 'Primary', 'url': entry.get('poster_url'),
                              'status': status, 'error': entry.get('error'), 'timestamp': entry.get('timestamp')}
                    self._insert_outcome(db, item, target, entry.get('operation', 'legacy'))
                for season in entry.get('season_results') or []:
                    if season.get('season_id'):
                        target = {'target_id': season['season_id'], 'title': season.get('season_title', 'Season'),
                                  'url': season.get('poster_url'), 'status': 'success' if season.get('success') else 'failed',
                                  'error': season.get('error'), 'timestamp': entry.get('timestamp')}
                        self._insert_outcome(db, item, target, entry.get('operation', 'legacy'))
            db.execute("INSERT INTO settings VALUES ('migration', 'legacy-v1', 'true')")
