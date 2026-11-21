"""Flask adapters for the shared poster service and durable job queue."""
import atexit
import gzip
import logging
import os
import signal
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import poster_scraper as scraper
from jobs import JobQueue
from picker_cache import PickerCache
from poster_service import PosterService
from safe_http import fetch_image, normalize_tpdb_page, origin, validate_image_url
from state_store import StateStore, timestamp

Config = scraper.Config
LIBRARY_FRESH_SEC = 30
LIBRARY_STALE_SEC = 600
GZIP_MIMETYPES = {'text/html', 'application/json'}
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 5


def create_app(config=None, store=None, job_queue=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config.update(config or {})
    app.config.update(MAX_CONTENT_LENGTH=1024 * 1024, SESSION_COOKIE_SAMESITE='Strict')
    state_dir = app.config.get('APP_STATE_DIR', 'data')
    cache_dir = app.config.get('CACHE_DIR', 'cache')
    if store is None:
        store = StateStore(os.path.join(state_dir, 'poster_manager.sqlite3'))
        legacy = dict(app.config)
        for key, default in {
            'PROTECTED_ITEMS_FILE': os.path.join(state_dir, 'protected_items.json'),
            'TPDB_ITEM_MAP_FILE': os.path.join(state_dir, 'tpdb_item_map.json'),
            'RESULTS_LOG_FILE': os.path.join(app.config.get('LOG_DIR', 'logs'), 'results.log'),
            'FAILED_LOG_FILE': os.path.join(app.config.get('LOG_DIR', 'logs'), 'failed.log'),
        }.items():
            legacy.setdefault(key, default)
        store.import_legacy(legacy)
    service = job_queue.service if job_queue else PosterService(store)
    queue = job_queue or JobQueue(store, service, delay=app.config.get('TPDB_BATCH_DELAY_SEC', 1.5))
    cache = PickerCache(os.path.join(cache_dir, 'picker-v2'), app.config.get('TPDB_PICKER_CACHE_MAX_AGE_DAYS', 7))
    app.extensions.update(poster_store=store, poster_service=service, poster_jobs=queue)
    if job_queue is None:
        atexit.register(queue.close)
    # Listing a large Jellyfin library takes seconds. Reuse a listing for LIBRARY_FRESH_SEC; the home
    # page also shows one up to LIBRARY_STALE_SEC old while it refreshes in the background. Items are
    # kept in Jellyfin's order and sorted per request. An artwork upload makes the listing stale
    # (its image tags change).
    library = {'items': None, 'libraries': [], 'fetched': 0.0, 'uploads': None}
    library_lock = threading.Lock()  # One Jellyfin fetch at a time.

    def library_age():
        if library['items'] is None or library['uploads'] != scraper.jellyfin_upload_count:
            return None
        return time.monotonic() - library['fetched']

    def load_library(max_age=LIBRARY_FRESH_SEC):
        with library_lock:
            age = library_age()
            if age is None or age >= max_age:
                uploads = scraper.jellyfin_upload_count
                libraries = scraper.get_jellyfin_libraries()
                all_items = scraper.get_jellyfin_items(sort_by=None, libraries=libraries)
                library.update(items=all_items, libraries=libraries, fetched=time.monotonic(), uploads=uploads)
            return library['libraries'], library['items']

    def refresh_library_in_background():
        def refresh():
            try:
                load_library()
            except Exception:
                logging.exception("Background Jellyfin library refresh failed")
        if not library_lock.locked():
            threading.Thread(target=refresh, name='library-refresh', daemon=True).start()

    def items(refresh=False):
        return load_library(0 if refresh else LIBRARY_FRESH_SEC)[1]

    def find_item(item_id):
        # A lookup by ID doesn't need fresh image tags, so a listing from before the latest upload
        # (or up to LIBRARY_STALE_SEC old) answers it at once and refreshes in the background;
        # clicks must not wait on a full Jellyfin listing. Unknown IDs still get a fresh listing.
        listing = library['items']
        if listing is not None and time.monotonic() - library['fetched'] < LIBRARY_STALE_SEC:
            item = next((item for item in listing if item['id'] == item_id), None)
            if item:
                age = library_age()
                if age is None or age >= LIBRARY_FRESH_SEC:
                    refresh_library_in_background()
                return item
        item = next((item for item in items() if item['id'] == item_id), None)
        if not item:
            raise LookupError('Jellyfin item not found')
        return item

    def body():
        data = request.get_json(silent=False) if request.data else {}
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data

    def boolean(data, key, default=False):
        value = data.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f'{key} must be a boolean')
        return value

    def item_ids(data, key='item_ids', default=None):
        value = data.get(key, default)
        if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value)):
            raise ValueError(f'{key} must be a list of item IDs')
        return value

    def public_job(job, results=True):
        if not job:
            return None
        result = {key: value for key, value in job.items() if key not in ('tasks', 'options')}
        if not results:
            # The job list is polled every few seconds; results load per job when they're needed.
            result['result_count'] = len(result.pop('results', None) or [])
        result['resumable'] = job.get('status') in ('interrupted', 'cancelled', 'failed') and (
            not job.get('prepared') or any('targets' not in task or any(
                target['status'] == 'pending' for target in task['targets']
            ) for task in job.get('tasks', []))
        )
        return result

    def auto_options(data):
        target_filter = data.get('filter', 'no-poster')
        if target_filter not in ('all', 'queued', 'no-poster', 'movies', 'series', 'collections', 'pending-seasons'):
            raise ValueError('Unknown batch filter')
        return {
            'filter': target_filter, 'library_id': data.get('library_id') or '',
            'item_ids': item_ids(data, default=[]),
            'skip_processed': boolean(data, 'skip_processed'),
            'include_season_posters': boolean(data, 'include_season_posters'),
            'replace_existing_season_posters': boolean(data, 'replace_existing_season_posters'),
            'save_placeholder_seasons': boolean(data, 'save_placeholder_seasons'),
            'include_collection_movies': boolean(data, 'include_collection_movies'),
            # Snapshot so a resumed job keeps choosing the same uploaders.
            'preferred_uploaders': service.preferred_uploaders(),
        }

    def manual_tasks(ids, confirm_protected=False, direct=None):
        selections = store.selections()
        tasks = []
        for item_id in ids:
            saved = selections.get(item_id)
            item = find_item(item_id)
            selection = direct if direct is not None else saved['selection'] if saved else None
            if item_id in store.values('protected') and not confirm_protected:
                raise PermissionError('Protected item: explicitly confirm the manual change')
            targets = service.targets(item, selection)
            service.validate_seasons(item, targets)
            tasks.append({'item': item, 'selection': selection, 'targets': targets})
        if not tasks:
            raise ValueError('No posters selected')
        return tasks

    def submit_manual(data, ids, direct=None):
        confirmed = boolean(data, 'confirm_protected')
        tasks = manual_tasks(ids, confirmed, direct)
        protected_ids = store.values('protected')
        return queue.submit('manual', tasks=tasks, options={
            'confirmed_protected_ids': [item_id for item_id in ids if confirmed and item_id in protected_ids],
        })

    def submit_retry(ids=None):
        tasks = service.retry_tasks(ids)
        if not tasks:
            raise ValueError('No retryable targets. Choose posters manually for items needing review.')
        return queue.submit('retry', tasks=tasks)

    def job_response(job, wait=False, single=False):
        # Legacy synchronous endpoints wait for the SAME worker; no second upload path.
        if wait:
            while not job['done']:
                time.sleep(0.1)
                job = store.get_job(job['job_id'])
            if single and job['results']:
                result = job['results'][0]
                return jsonify(result), 200 if result['success'] else 409
            return jsonify(public_job(job))
        return jsonify(success=True, job_id=job['job_id'], job=public_job(job)), 202

    @app.before_request
    def same_origin_only():
        # Local-only by default, including protection from DNS rebinding.
        if app.config.get('WEB_HOST', '127.0.0.1') in ('localhost', '127.0.0.1', '::1'):
            if urlsplit(request.host_url).hostname not in ('localhost', '127.0.0.1', '::1'):
                return jsonify(error='Local host required'), 403
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(error='Cross-site requests are not allowed'), 403
        request_origin = request.headers.get('Origin')
        if request_origin and origin(request_origin) != origin(request.host_url):
            return jsonify(error='Cross-origin requests are not allowed'), 403

    @app.after_request
    def gzip_response(response):
        # The home page lists the whole library (tens of MB for big ones) and is very repetitive markup.
        if (response.mimetype not in GZIP_MIMETYPES or response.direct_passthrough or response.is_streamed
                or 'Content-Encoding' in response.headers
                or 'gzip' not in request.headers.get('Accept-Encoding', '').lower()):
            return response
        data = response.get_data()
        if len(data) >= GZIP_MIN_BYTES:
            response.set_data(gzip.compress(data, compresslevel=GZIP_LEVEL))
            response.headers['Content-Encoding'] = 'gzip'
        response.vary.add('Accept-Encoding')
        return response

    @app.errorhandler(Exception)
    def api_error(error):
        if isinstance(error, HTTPException):
            return jsonify(success=False, error=error.description), error.code
        if isinstance(error, scraper.TPDBRateLimited):
            status = 429
        elif isinstance(error, scraper.BrowserUnavailable):
            logging.error('%s', error)
            status = 503
        elif isinstance(error, PermissionError):
            status = 403
        elif isinstance(error, LookupError):
            status = 404
        elif isinstance(error, ValueError):
            status = 400
        else:
            status = 500
        if status == 500:
            logging.exception('Poster manager request failed')
        return jsonify(success=False, error=str(error)), status

    @app.route('/')
    def index():
        item_type = request.args.get('type')
        current_library = request.args.get('library')
        sort_by = request.args.get('sort', 'library')
        error = None
        try:
            server_info = scraper.get_jellyfin_server_info()
            age = library_age()
            if age is not None and age < LIBRARY_STALE_SEC:
                libraries, cached_items = library['libraries'], library['items']
                if age >= LIBRARY_FRESH_SEC:
                    refresh_library_in_background()
            else:
                libraries, cached_items = load_library()
            if libraries:
                all_items = scraper.sort_jellyfin_items(list(cached_items), sort_by)
            else:
                # Without libraries Jellyfin does the sorting, so the order depends on sort_by.
                all_items = scraper.get_jellyfin_items(sort_by=sort_by, libraries=libraries)
            if not server_info.get('connected', True):
                error = 'Could not connect to Jellyfin. Check the server URL and API key.'
        except Exception as exc:
            server_info, libraries, all_items = {'name': 'Jellyfin Server'}, [], []
            error = str(exc)
        # The page arrives already filtered, instead of showing every card until the script hides them.
        dom_type = {'movies': 'movie', 'series': 'series', 'collections': 'collection'}.get(item_type)
        of_type = [item for item in all_items if not dom_type or item.get('type', '').lower() == dom_type]
        if current_library not in {item.get('library_id') for item in of_type}:
            current_library = None  # Same as the page script: a library without items of this type isn't selected.
        shown = [item for item in of_type if not current_library or item.get('library_id') == current_library]
        return render_template('index.html', items=all_items, libraries=libraries,
                               server_info=server_info, current_filter=item_type,
                               current_library=current_library, current_sort=sort_by, error=error,
                               shown_ids={item['id'] for item in shown},
                               shown_libraries={item.get('library_id') or '' for item in shown})

    @app.route('/selections')
    def selections():
        return jsonify(selections={key: value['selection'] for key, value in store.selections().items()},
                       queued_item_ids=list(store.values('queue')))

    @app.route('/queue/<item_id>', methods=['POST'])
    def queue_item(item_id):
        find_item(item_id)
        queued = boolean(body(), 'queued')
        store.set_value('queue', item_id, True if queued else None)
        return jsonify(success=True)

    @app.route('/item/<item_id>/select', methods=['POST'])
    def select_poster(item_id):
        data = body()
        item = find_item(item_id)
        if boolean(data, 'clear_selection'):
            store.set_selection(item, None)
        else:
            selection = data.get('selection') or data.get('poster_url')
            targets = service.targets(item, selection)
            service.validate_seasons(item, targets)
            store.set_selection(item, selection)
        return jsonify(success=True)

    @app.route('/item/<item_id>/posters')
    def get_item_posters(item_id):
        item = find_item(item_id)
        seasons = scraper.get_jellyfin_seasons(item_id) if item.get('type') == 'Series' else []
        limit = max(1, min(request.args.get('set_limit', default=3, type=int), Config.MAX_POSTERS_PER_ITEM))
        override = normalize_tpdb_page(request.args.get('tpdb_url'))
        set_url = normalize_tpdb_page(request.args.get('set_url'), 'set')
        # Disabling the cache never disables an authoritative user correction.
        mapping = override or normalize_tpdb_page(store.values('mapping').get(item_id, ''))
        use_cache = request.args.get('use_cache', 'true').lower() != 'false'
        preferred = service.preferred_uploaders()
        key = cache.key({'item': item, 'seasons': seasons, 'mapping': mapping, 'limit': limit, 'set': set_url,
                         'preferred_uploaders': preferred})
        cached = cache.get(key) if use_cache and not override else None
        if cached:
            return jsonify(dict(cached, from_cache=True))
        if request.args.get('cache_only') == 'true':
            return jsonify(cache_miss=True, from_cache=False)
        result = scraper.search_tpdb_for_poster_groups(
            item['title'], item_year=item.get('year'), item_type=item.get('type'),
            tmdb_id=item.get('ProviderIds', {}).get('Tmdb'), eligible_seasons=seasons,
            max_posters=limit if item.get('type') == 'Series' else Config.MAX_POSTERS_PER_ITEM,
            requested_set_urls=[set_url] if set_url else None, tpdb_item_url=mapping,
            include_base64=False, preferred_uploaders=preferred,
        )
        if override:
            store.set_value('mapping', item_id, override)
        resolved = mapping or (result.get('best_group') or {}).get('url') or ''
        payload = dict(item=item, posters=result.get('posters', []), poster_groups=result.get('groups', []),
                       eligible_seasons=seasons, poster_set_limit=limit,
                       can_browse_more_sets=item.get('type') == 'Series' and limit < Config.MAX_POSTERS_PER_ITEM,
                       tpdb_mapping_url=resolved, from_cache=False)
        if use_cache:
            cache.put(key, payload)
        return jsonify(payload)

    @app.route('/settings/preferred-uploaders', methods=['GET', 'PUT'])
    def preferred_uploaders():
        if request.method == 'PUT':
            names = service.set_preferred_uploaders(body().get('uploaders', []))
            return jsonify(success=True, uploaders=names)
        return jsonify(uploaders=service.preferred_uploaders())

    @app.route('/item/<item_id>/collection-set')
    def get_collection_set(item_id):
        return jsonify(service.collection_set_preview(find_item(item_id), request.args.get('set_url')))

    @app.route('/pending-seasons', methods=['GET', 'DELETE'])
    def pending_seasons():
        if request.method == 'DELETE':
            key = request.args.get('key')
            entries = service.pending_seasons()
            keys = [entry['key'] for entry in entries if not key or entry['key'] == key]
            if key and not keys:
                raise LookupError('Unknown placeholder')
            for entry_key in keys:
                service.clear_pending_season(entry_key)
            return jsonify(success=True, removed=len(keys))
        return jsonify(pending=service.pending_seasons())

    @app.route('/item/<item_id>/seasons')
    def get_item_seasons(item_id):
        item = find_item(item_id)
        return jsonify(item=item, seasons=scraper.get_jellyfin_seasons(item_id) if item.get('type') == 'Series' else [])

    @app.route('/item/<item_id>/season-count')
    def get_item_season_count(item_id):
        item = find_item(item_id)
        return jsonify(season_count=len(scraper.get_jellyfin_seasons(item_id)) if item.get('type') == 'Series' else None)

    @app.route('/item/<item_id>/artwork')
    def artwork(item_id):
        # No stale image tag: fresh proxy URL used after a successful primary upload.
        item = find_item(item_id)
        url = f"{Config.JELLYFIN_URL}/Items/{item['id']}/Images/Primary?maxWidth=300&quality=85"
        return jsonify(url=url)

    @app.route('/jellyfin-image')
    def get_jellyfin_image():
        url = request.args.get('url')
        if not url:
            return placeholder()
        validate_image_url(url, 'jellyfin')
        image, content_type = fetch_image(url, 'jellyfin', headers=scraper.get_jellyfin_headers())
        return image_response(image, content_type)

    @app.route('/thumbnail')
    def get_thumbnail():
        url = request.args.get('url')
        if not url or url == 'None':
            return placeholder()
        validate_image_url(url, 'tpdb')
        image, content_type = scraper.fetch_tpdb_image(url)
        return image_response(image, content_type)

    def image_response(image, content_type):
        return Response(image, content_type=content_type,
                        headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})

    def placeholder():
        return app.send_static_file('images/no-poster.svg')

    @app.route('/health/live')
    def liveness_check():
        return jsonify(status='alive'), 200

    @app.route('/health')
    def health_check():
        info = scraper.get_jellyfin_server_info()
        connected = info.get('connected', False)
        return jsonify(status='healthy' if connected else 'degraded', timestamp=timestamp(),
                       jellyfin_status='connected' if connected else 'disconnected',
                       server_name=info.get('name'), server_version=info.get('version'),
                       selenium_active=scraper.selenium_driver is not None), 200 if connected else 503

    @app.route('/debug/tpdb-search')
    def debug_tpdb_search():
        if not app.debug:
            return jsonify(error='Not found'), 404
        title = request.args.get('title', '').strip()
        if not title:
            raise ValueError('Missing title')
        result = scraper.search_tpdb_for_poster_groups(
            title, item_year=request.args.get('year', type=int), item_type=request.args.get('type'),
            tmdb_id=request.args.get('tmdb_id'),
            max_posters=max(1, min(request.args.get('max_posters', default=3, type=int), 18)),
            include_base64=False,
        )
        return jsonify(success=True, selenium_url=scraper._get_selenium_current_url(), **result)

    @app.route('/jobs', methods=['GET', 'POST'])
    def jobs():
        if request.method == 'GET':
            all_jobs = store.jobs()
            open_jobs = [job for job in all_jobs if not job['done'] or public_job(job)['resumable']]
            finished = [job for job in all_jobs if job['done'] and not public_job(job)['resumable']][:100]
            return jsonify(jobs=[public_job(job, results=False) for job in open_jobs + finished])
        data = body()
        kind = data.get('kind')
        if kind == 'auto':
            job = queue.submit('auto', options=auto_options(data))
        elif kind == 'manual':
            job = submit_manual(data, item_ids(data, default=list(store.selections())))
        elif kind == 'retry':
            job = submit_retry(item_ids(data))
        else:
            raise ValueError('Unknown job kind')
        return job_response(job)

    @app.route('/batch-auto-poster/start', methods=['POST'])
    def start_batch_auto_poster():
        return job_response(queue.submit('auto', options=auto_options(body())))

    @app.route('/batch-auto-poster/progress/<job_id>')
    @app.route('/jobs/<job_id>')
    def batch_auto_poster_progress(job_id):
        job = store.get_job(job_id)
        if not job:
            raise LookupError('Job not found')
        return jsonify(success=True, job=public_job(job))

    @app.route('/jobs/<job_id>/results')
    def job_results(job_id):
        job = store.get_job(job_id)
        if not job:
            raise LookupError('Job not found')
        start = max(request.args.get('start', 0, type=int), 0)
        return jsonify(success=True, results=job.get('results', [])[start:])

    @app.route('/batch-auto-poster/cancel/<job_id>', methods=['POST'])
    @app.route('/jobs/<job_id>/cancel', methods=['POST'])
    def cancel_batch_auto_poster(job_id):
        return jsonify(success=True, job=public_job(queue.cancel(job_id)))

    @app.route('/jobs/<job_id>/resume', methods=['POST'])
    def resume_job(job_id):
        return job_response(queue.resume(job_id))

    @app.route('/batch-auto-poster/latest-results')
    def latest_batch_auto_poster_results():
        latest = next((job for job in store.jobs() if job['done']), None)
        if not latest:
            history = store.processed_items(limit=100)
            if history:
                latest = dict(job_id='processed-history', done=True, status='completed', success=True,
                              results=[dict(entry, success=True) for entry in history])
        return jsonify(success=True, job=public_job(latest))

    @app.route('/batch-auto-poster', methods=['POST'])
    def batch_auto_poster():
        return job_response(queue.submit('auto', options=auto_options(body())), wait=True)

    @app.route('/upload/<item_id>', methods=['POST'])
    def upload_poster(item_id):
        return job_response(submit_manual(body(), [item_id]), wait=True, single=True)

    @app.route('/upload-all', methods=['POST'])
    def upload_all_selected():
        return job_response(submit_manual(body(), list(store.selections())), wait=True)

    @app.route('/upload-poster', methods=['POST'])
    def upload_poster_direct():
        data = body()
        if not data.get('item_id') or not data.get('poster_url'):
            raise ValueError('Missing item_id or poster_url')
        return job_response(submit_manual(data, [data['item_id']], direct=data['poster_url']), wait=True, single=True)

    @app.route('/failed-items', methods=['GET', 'DELETE'])
    def failed_items():
        if request.method == 'DELETE':
            store.clear_history('failed')
            return jsonify(success=True, items=[])
        return jsonify(items=store.failed_items(limit=max(1, min(request.args.get('limit', default=100, type=int), 500))))

    @app.route('/processed-items', methods=['GET', 'DELETE'])
    def processed_items():
        if request.method == 'DELETE':
            return jsonify(success=True, removed_count=store.clear_history('success', item_ids(body())))
        return jsonify(items=store.processed_items(limit=max(1, min(request.args.get('limit', default=500, type=int), 1000))))

    @app.route('/failed-items/retry', methods=['POST'])
    def retry_failed_item():
        data = body()
        if not data.get('item_id'):
            raise ValueError('Missing item_id')
        return job_response(submit_retry([data['item_id']]), wait=True, single=True)

    @app.route('/failed-items/retry-all', methods=['POST'])
    def retry_all_failed_items():
        limit = max(1, min(int(body().get('limit', 100)), 500))
        ids = [entry['item_id'] for entry in store.failed_items(limit=limit)]
        return job_response(submit_retry(ids), wait=True)

    @app.route('/protected-items')
    def protected_items():
        return jsonify(items=list(store.values('protected')))

    @app.route('/protected-items/toggle', methods=['POST'])
    def toggle_protected_item():
        data = body()
        item_id = data.get('item_id')
        find_item(item_id)
        protected = boolean(data, 'protected') if 'protected' in data else None
        protected = store.set_protected(item_id, protected)
        return jsonify(success=True, item_id=item_id, protected=protected, items=list(store.values('protected')))

    @app.route('/tpdb-cache', methods=['DELETE'])
    def clear_tpdb_cache():
        cache.clear()
        return jsonify(success=True)

    @app.route('/jellyfin-items')
    def jellyfin_items():
        all_items = scraper.get_jellyfin_items(item_type=request.args.get('type'), sort_by=request.args.get('sort', 'name'))
        return jsonify(items=all_items, total_count=len(all_items), server_info=scraper.get_jellyfin_server_info())

    return app


def handle_shutdown_signal(signum, frame):
    logging.info('Shutdown requested by signal %s', signum)
    raise SystemExit(0)


def main():
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if Config.DEBUG else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(Config.LOG_DIR, 'app.log'))])
    app = create_app()
    host = getattr(Config, 'WEB_HOST', '127.0.0.1')
    if host not in ('localhost', '127.0.0.1', '::1'):
        logging.warning('Non-local binding: put an authenticated reverse proxy in front of this single-user app.')
    previous_handler = signal.signal(signal.SIGTERM, handle_shutdown_signal)
    try:
        app.run(host=host, port=int(getattr(Config, 'WEB_PORT', 5001)), debug=Config.DEBUG, use_reloader=False)
    finally:
        try:
            app.extensions['poster_jobs'].close()
        finally:
            scraper.teardown_selenium()
            signal.signal(signal.SIGTERM, previous_handler)


if __name__ == '__main__':
    main()
