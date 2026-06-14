from flask import Flask, render_template, request, jsonify, session, Response
import uuid
import json
import os
import logging
import re
import hashlib
import base64
import sys
import time
from io import BytesIO
from datetime import datetime, timedelta, timezone
from poster_scraper import *
from config import Config
import threading

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

app = Flask(__name__)
app.config.from_object(Config)


def _utc_now():
    return datetime.now(timezone.utc)


def _utc_timestamp():
    return _utc_now().isoformat(timespec='seconds').replace('+00:00', 'Z')


def _compress_base64_preview(data_url):
    if not data_url or not isinstance(data_url, str) or not Image or not ImageOps:
        return data_url
    if not data_url.startswith('data:image/') or ';base64,' not in data_url:
        return data_url

    try:
        _, encoded = data_url.split(';base64,', 1)
        image_bytes = base64.b64decode(encoded)
        with Image.open(BytesIO(image_bytes)) as image:
            image = ImageOps.fit(image, TPDB_PREVIEW_MAX_SIZE, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            if image.mode not in ('RGB', 'RGBA'):
                image = image.convert('RGB')

            output = BytesIO()
            try:
                image.save(output, format='WEBP', quality=TPDB_PREVIEW_QUALITY, method=4)
                content_type = 'image/webp'
            except Exception:
                output = BytesIO()
                if image.mode == 'RGBA':
                    image = image.convert('RGB')
                image.save(output, format='JPEG', quality=TPDB_PREVIEW_QUALITY, optimize=True)
                content_type = 'image/jpeg'

        return f"data:{content_type};base64,{base64.b64encode(output.getvalue()).decode('utf-8')}"
    except Exception as e:
        logging.debug(f"Failed to compress cached TPDb preview: {e}")
        return data_url

class ConsoleFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[97;41m",
    }
    VALUE = "\033[96m"
    RESET = "\033[0m"
    ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
    VALUE_PREFIXES = (
        "Successfully uploaded poster for: ",
        "Searching posters for: ",
        "Processing item ",
        "No TPDb search results for ",
        "Found 0 poster links for ",
    )

    def __init__(self, use_color=True):
        super().__init__(datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record):
        timestamp = self.formatTime(record, self.datefmt)
        display_level = record.levelno
        message = record.getMessage()
        plain_message = self.ANSI_PATTERN.sub("", message).strip()

        if plain_message.startswith("WARNING:"):
            display_level = logging.WARNING
            message = plain_message.removeprefix("WARNING:").strip()

        level = logging.getLevelName(display_level).ljust(7)

        if self.use_color:
            color = self.COLORS.get(display_level, "")
            level = f"{color}{level}{self.RESET}"
            message = self.highlight_value(message)

        return f"{timestamp} {level} {message}"

    def highlight_value(self, message):
        for prefix in self.VALUE_PREFIXES:
            if message.startswith(prefix):
                return f"{prefix}{self.VALUE}{message[len(prefix):]}{self.RESET}"
        return message


class WerkzeugAccessLogFilter(logging.Filter):
    METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")

    def filter(self, record):
        if getattr(Config, "DEBUG", False):
            return True

        message = record.getMessage()
        is_access_log = " HTTP/" in message and any(f'"{method} ' in message for method in self.METHODS)
        return not is_access_log


def setup_logging():
    os.makedirs(Config.LOG_DIR, exist_ok=True)

    log_level = logging.DEBUG if Config.DEBUG else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    file_handler = logging.FileHandler(f'{Config.LOG_DIR}/app.log', encoding='utf-8')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    ))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(ConsoleFormatter(
        use_color=sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
    ))

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.handlers.clear()
    werkzeug_logger.setLevel(log_level)
    werkzeug_logger.addFilter(WerkzeugAccessLogFilter())
    werkzeug_logger.propagate = True


setup_logging()

# Global storage for session data
user_sessions = {}
selenium_ready_event = threading.Event()
BATCH_DELAY_SEC = getattr(Config, 'TPDB_BATCH_DELAY_SEC', 1.5)
FAILED_LOG_FILE = getattr(Config, 'FAILED_LOG_FILE', os.path.join(Config.LOG_DIR, 'failed.log'))
RESULTS_LOG_FILE = getattr(Config, 'RESULTS_LOG_FILE', os.path.join(Config.LOG_DIR, 'results.log'))
CACHE_DIR = getattr(Config, 'CACHE_DIR', 'cache')
APP_STATE_DIR = getattr(Config, 'APP_STATE_DIR', 'data')
PROTECTED_ITEMS_FILE = getattr(Config, 'PROTECTED_ITEMS_FILE', os.path.join(APP_STATE_DIR, 'protected_items.json'))
TPDB_ITEM_MAP_FILE = getattr(Config, 'TPDB_ITEM_MAP_FILE', os.path.join(APP_STATE_DIR, 'tpdb_item_map.json'))
if getattr(Config, 'TEMP_POSTER_DIR', None) in (None, 'temp_posters'):
    Config.TEMP_POSTER_DIR = os.path.join(CACHE_DIR, 'temp_posters')
TPDB_SET_CACHE_FILE = getattr(Config, 'TPDB_SET_CACHE_FILE', os.path.join(CACHE_DIR, 'tpdb_set_cache.json'))
TPDB_PICKER_CACHE_FILE = getattr(Config, 'TPDB_PICKER_CACHE_FILE', os.path.join(CACHE_DIR, 'tpdb_picker_cache.json'))
TPDB_SET_CACHE_MAX_AGE_DAYS = getattr(Config, 'TPDB_SET_CACHE_MAX_AGE_DAYS', 14)
TPDB_PICKER_CACHE_MAX_AGE_DAYS = getattr(Config, 'TPDB_PICKER_CACHE_MAX_AGE_DAYS', 7)
auto_batch_jobs = {}
auto_batch_jobs_lock = threading.Lock()
latest_auto_batch_job_id = None
season_count_cache = {}
season_count_cache_lock = threading.Lock()
MAX_FINISHED_AUTO_BATCH_JOBS = 20
MAX_SEASON_COUNT_CACHE_ENTRIES = 2000


def _prune_auto_batch_jobs():
    # Caller must hold auto_batch_jobs_lock.
    finished = [job_id for job_id, job in auto_batch_jobs.items() if job.get('done')]
    for job_id in finished[:-MAX_FINISHED_AUTO_BATCH_JOBS]:
        del auto_batch_jobs[job_id]


def _prune_season_count_cache():
    # Caller must hold season_count_cache_lock.
    excess = len(season_count_cache) - MAX_SEASON_COUNT_CACHE_ENTRIES
    if excess > 0:
        for key in list(season_count_cache.keys())[:excess]:
            del season_count_cache[key]


def _evict_stale_user_sessions(max_age_sec=7200):
    now_ts = time.time()
    stale_session_ids = [
        session_id
        for session_id, session_data in list(user_sessions.items())
        if now_ts - session_data.get('last_seen', now_ts) > max_age_sec
    ]
    for stale_session_id in stale_session_ids:
        user_sessions.pop(stale_session_id, None)
    if stale_session_ids:
        logging.info("Evicted %d stale user sessions.", len(stale_session_ids))


def _touch_session(session_id):
    if session_id in user_sessions:
        user_sessions[session_id]['last_seen'] = time.time()


def _sweep_stale_temp_posters(max_age_sec=3600):
    if not os.path.isdir(Config.TEMP_POSTER_DIR):
        return

    now_ts = time.time()
    removed_count = 0
    for file_name in os.listdir(Config.TEMP_POSTER_DIR):
        if not (
            (file_name.startswith("auto_") or file_name.startswith("manual_"))
            and file_name.lower().endswith(".jpg")
        ):
            continue
        file_path = os.path.join(Config.TEMP_POSTER_DIR, file_name)
        try:
            if now_ts - os.path.getmtime(file_path) > max_age_sec:
                os.remove(file_path)
                removed_count += 1
        except Exception as cleanup_error:
            logging.warning(f"Failed to sweep stale temp file {file_path}: {cleanup_error}")
    if removed_count:
        logging.info("Removed %d stale temp poster files.", removed_count)


def _create_auto_batch_job(target_filter, skip_processed=False, include_season_posters=False, replace_existing_season_posters=False, item_ids=None):
    job_id = str(uuid.uuid4())
    now = _utc_timestamp()
    job = {
        'job_id': job_id,
        'filter': target_filter,
        'skip_processed': skip_processed,
        'include_season_posters': include_season_posters,
        'replace_existing_season_posters': replace_existing_season_posters,
        'item_ids': item_ids or [],
        'status': 'starting',
        'phase': 'starting',
        'message': 'Starting automatic poster batch...',
        'cancel_requested': False,
        'current_item': None,
        'current_item_id': None,
        'current_item_type': None,
        'current_item_year': None,
        'old_poster_url': None,
        'new_poster_url': None,
        'total_items': 0,
        'processed': 0,
        'remaining': 0,
        'successful': 0,
        'failed': 0,
        'results': [],
        'done': False,
        'success': None,
        'error': None,
        'created_at': now,
        'updated_at': now,
    }
    with auto_batch_jobs_lock:
        _prune_auto_batch_jobs()
        auto_batch_jobs[job_id] = job
    return job_id


def _update_auto_batch_job(job_id, **updates):
    global latest_auto_batch_job_id
    updates['updated_at'] = _utc_timestamp()
    with auto_batch_jobs_lock:
        job = auto_batch_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        if 'processed' in updates or 'total_items' in updates:
            job['remaining'] = max(job.get('total_items', 0) - job.get('processed', 0), 0)
        if updates.get('done'):
            latest_auto_batch_job_id = job_id


def _get_auto_batch_job(job_id):
    with auto_batch_jobs_lock:
        job = auto_batch_jobs.get(job_id)
        if not job:
            return None
        snapshot = dict(job)
        snapshot['results'] = list(job.get('results', []))
        return snapshot


def _get_latest_auto_batch_job():
    with auto_batch_jobs_lock:
        if not latest_auto_batch_job_id:
            return None
        job = auto_batch_jobs.get(latest_auto_batch_job_id)
        if not job:
            return None
        snapshot = dict(job)
        snapshot['results'] = list(job.get('results', []))
        return snapshot


def _is_auto_batch_cancelled(job_id):
    with auto_batch_jobs_lock:
        job = auto_batch_jobs.get(job_id)
        return bool(job and job.get('cancel_requested'))


def _cancel_auto_batch_job(job_id):
    with auto_batch_jobs_lock:
        job = auto_batch_jobs.get(job_id)
        if not job:
            return None
        if job.get('done'):
            return dict(job)
        job['cancel_requested'] = True
        job['status'] = 'cancelling'
        job['phase'] = 'cancelling'
        job['message'] = 'Cancelling after the current step...'
        job['updated_at'] = _utc_timestamp()
        return dict(job)


def _finish_auto_batch_cancelled(job_id, results, successful_count, failed_count):
    _update_auto_batch_job(
        job_id,
        status='cancelled',
        phase='cancelled',
        message='Automatic batch cancelled.',
        current_item=None,
        current_item_id=None,
        old_poster_url=None,
        new_poster_url=None,
        results=list(results),
        successful=successful_count,
        failed=failed_count,
        done=True,
        success=False,
        error='Cancelled by user',
    )


def _get_failed_log_path():
    return FAILED_LOG_FILE


def _get_results_log_path():
    return RESULTS_LOG_FILE


def _get_protected_items_path():
    return PROTECTED_ITEMS_FILE


def _get_tpdb_item_map_path():
    return TPDB_ITEM_MAP_FILE


def _get_tpdb_set_cache_path():
    return TPDB_SET_CACHE_FILE


def _get_tpdb_picker_cache_path():
    return TPDB_PICKER_CACHE_FILE


def _ensure_parent_dir(path):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)


def _normalize_tpdb_item_url(value):
    value = (value or '').strip()
    if not value:
        return ''
    if value.isdigit():
        return f"{Config.TPDB_BASE_URL}/posters/{value}"
    match = re.search(r'(?:https?://theposterdb\.com)?/posters/(\d+)', value, re.IGNORECASE)
    if match:
        return f"{Config.TPDB_BASE_URL}/posters/{match.group(1)}"
    return ''


def _build_tpdb_set_url(set_id):
    return f"{Config.TPDB_BASE_URL}/set/{set_id}" if set_id else ''


def _read_tpdb_item_map():
    path = _get_tpdb_item_map_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as map_file:
            data = json.load(map_file)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"Failed to read TPDb item map: {e}")
        return {}


def _write_tpdb_item_map(mapping):
    _ensure_parent_dir(_get_tpdb_item_map_path())
    payload = {
        str(item_id): _normalize_tpdb_item_url(url)
        for item_id, url in (mapping or {}).items()
        if item_id and _normalize_tpdb_item_url(url)
    }
    with open(_get_tpdb_item_map_path(), 'w', encoding='utf-8') as map_file:
        json.dump(payload, map_file, ensure_ascii=False, indent=2)


def _get_tpdb_item_map_url(item_id):
    return _normalize_tpdb_item_url(_read_tpdb_item_map().get(str(item_id)))


def _set_tpdb_item_map_url(item_id, tpdb_url):
    tpdb_url = _normalize_tpdb_item_url(tpdb_url)
    if not item_id or not tpdb_url:
        return ''
    mapping = _read_tpdb_item_map()
    mapping[str(item_id)] = tpdb_url
    _write_tpdb_item_map(mapping)
    return tpdb_url


def _read_tpdb_set_cache():
    path = _get_tpdb_set_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as cache_file:
            data = json.load(cache_file)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"Failed to read TPDb set cache: {e}")
        return {}


def _write_tpdb_set_cache(cache):
    os.makedirs(os.path.dirname(_get_tpdb_set_cache_path()) or '.', exist_ok=True)
    with open(_get_tpdb_set_cache_path(), 'w', encoding='utf-8') as cache_file:
        json.dump(cache or {}, cache_file, ensure_ascii=False, indent=2)


def _is_tpdb_set_cache_fresh(entry):
    updated_at = (entry or {}).get('updated_at')
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return _utc_now() - updated <= timedelta(days=TPDB_SET_CACHE_MAX_AGE_DAYS)


def _get_cached_tpdb_sets(tpdb_item_url):
    tpdb_item_url = _normalize_tpdb_item_url(tpdb_item_url)
    if not tpdb_item_url:
        return []
    entry = _read_tpdb_set_cache().get(tpdb_item_url)
    if not _is_tpdb_set_cache_fresh(entry):
        return []
    sets = []
    for set_info in entry.get('available_sets', []):
        set_id = str(set_info.get('set_id') or '')
        if not set_id:
            continue
        sets.append({
            'set_id': set_id,
            'set_url': _build_tpdb_set_url(set_id),
            'set_poster_count': set_info.get('set_poster_count'),
            'uploader': set_info.get('uploader') or 'Unknown',
            'preview_base64': set_info.get('preview_base64'),
        })
    return sets


def _cache_tpdb_sets_from_groups(groups):
    cache = _read_tpdb_set_cache()
    changed = False
    for group in groups or []:
        tpdb_item_url = _normalize_tpdb_item_url(group.get('url'))
        available_sets = group.get('available_sets') or []
        if not tpdb_item_url or not available_sets:
            continue
        cache[tpdb_item_url] = {
            'updated_at': _utc_timestamp(),
            'available_sets': [
                {
                    'set_id': str(set_info.get('set_id') or ''),
                    'set_poster_count': set_info.get('set_poster_count'),
                    'uploader': set_info.get('uploader') or 'Unknown',
                    'preview_base64': _compress_base64_preview(set_info.get('preview_base64')),
                }
                for set_info in available_sets
                if set_info.get('set_id')
            ],
        }
        changed = True
    if changed:
        _write_tpdb_set_cache(cache)


def _read_tpdb_picker_cache():
    path = _get_tpdb_picker_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as cache_file:
            data = json.load(cache_file)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"Failed to read TPDb picker cache: {e}")
        return {}


def _write_tpdb_picker_cache(cache):
    os.makedirs(os.path.dirname(_get_tpdb_picker_cache_path()) or '.', exist_ok=True)
    with open(_get_tpdb_picker_cache_path(), 'w', encoding='utf-8') as cache_file:
        json.dump(cache or {}, cache_file, ensure_ascii=False, indent=2)


def _is_tpdb_picker_cache_fresh(entry):
    updated_at = (entry or {}).get('updated_at')
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return _utc_now() - updated <= timedelta(days=TPDB_PICKER_CACHE_MAX_AGE_DAYS)


def _tpdb_picker_cache_key(item, tpdb_url, poster_set_limit, eligible_seasons):
    season_signature = [
        {
            'id': str(season.get('id') or ''),
            'number': season.get('number'),
            'has_poster': bool(season.get('has_poster')),
        }
        for season in (eligible_seasons or [])
    ]
    payload = {
        'item_id': str(item.get('id') or ''),
        'item_type': item.get('type') or '',
        'item_year': item.get('year'),
        'tpdb_url': _normalize_tpdb_item_url(tpdb_url),
        'poster_set_limit': poster_set_limit,
        'seasons': season_signature,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def _get_cached_tpdb_picker_response(cache_key):
    entry = _read_tpdb_picker_cache().get(cache_key)
    if not _is_tpdb_picker_cache_fresh(entry):
        return None
    response = _hydrate_tpdb_picker_cache_response(entry.get('response'))
    return response if isinstance(response, dict) else None


def _cache_tpdb_picker_response(cache_key, response_payload):
    if not cache_key or not response_payload:
        return
    cache = _read_tpdb_picker_cache()
    cache[cache_key] = {
        'updated_at': _utc_timestamp(),
        'response': _compact_tpdb_picker_cache_response(response_payload),
    }
    _write_tpdb_picker_cache(cache)


def _clear_tpdb_cache():
    cleared = []
    for path in (_get_tpdb_set_cache_path(), _get_tpdb_picker_cache_path()):
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w', encoding='utf-8') as cache_file:
                json.dump({}, cache_file)
            cleared.append(path)
        except Exception as e:
            logging.warning(f"Failed to clear TPDb cache file {path}: {e}")
    return cleared


def _compact_tpdb_picker_cache_response(response):
    if not isinstance(response, dict):
        return response

    compact_groups = [_compact_tpdb_group(group) for group in response.get('poster_groups', [])]
    compact_posters = [_compact_tpdb_poster(poster) for poster in response.get('posters', [])]
    first_group_posters = compact_groups[0].get('s', []) if compact_groups else []

    payload = {
        'poster_groups': compact_groups,
        'eligible_seasons': [_compact_eligible_season(season) for season in response.get('eligible_seasons', [])],
        'poster_set_limit': response.get('poster_set_limit'),
        'can_browse_more_sets': response.get('can_browse_more_sets'),
        'tpdb_mapping_url': response.get('tpdb_mapping_url'),
    }
    if compact_posters != first_group_posters:
        payload['posters'] = compact_posters

    return {
        key: value
        for key, value in payload.items()
        if value not in (None, '', [], {})
    }


def _hydrate_tpdb_picker_cache_response(response):
    if not isinstance(response, dict):
        return response

    poster_groups = [_hydrate_tpdb_group(group) for group in response.get('poster_groups', [])]
    posters = [_hydrate_tpdb_poster(poster) for poster in response.get('posters', [])]
    if not posters and poster_groups:
        posters = poster_groups[0].get('show_posters', [])

    return {
        'posters': posters,
        'poster_groups': poster_groups,
        'eligible_seasons': [_hydrate_eligible_season(season) for season in response.get('eligible_seasons', [])],
        'poster_set_limit': response.get('poster_set_limit'),
        'can_browse_more_sets': response.get('can_browse_more_sets'),
        'tpdb_mapping_url': response.get('tpdb_mapping_url'),
    }


def _compact_tpdb_group(group):
    expected_group_id = f"group-{group.get('source_index')}"
    compact = {
        'i': group.get('id') if group.get('id') != expected_group_id else None,
        'n': group.get('title'),
        'u': group.get('url'),
        'm': group.get('match_score'),
        'x': group.get('source_index'),
        's': [_compact_tpdb_poster(poster, group_id=group.get('id')) for poster in group.get('show_posters', [])],
        'p': [_compact_tpdb_poster(poster, group_id=group.get('id')) for poster in group.get('season_posters', [])],
        'e': group.get('eligible_season_count'),
        'c': group.get('covered_season_count'),
        'k': group.get('covered_season_keys') or [],
        'a': [_compact_available_set(set_info) for set_info in group.get('available_sets', [])],
    }
    return {key: value for key, value in compact.items() if value not in (None, '', [], {})}


def _hydrate_tpdb_group(group):
    group_id = group.get('i') or f"group-{group.get('x', 0)}"
    hydrated = {
        'id': group_id,
        'title': group.get('n'),
        'url': group.get('u'),
        'match_score': group.get('m'),
        'source_index': group.get('x'),
        'show_posters': [_hydrate_tpdb_poster(poster, group_id=group_id) for poster in group.get('s', [])],
        'season_posters': [_hydrate_tpdb_poster(poster, group_id=group_id) for poster in group.get('p', [])],
        'eligible_season_count': group.get('e', 0),
        'covered_season_count': group.get('c', 0),
        'covered_season_keys': group.get('k', []),
        'available_sets': [_hydrate_available_set(set_info) for set_info in group.get('a', [])],
    }
    return hydrated


def _compact_tpdb_poster(poster, group_id=None):
    compact = {
        'i': poster.get('id'),
        'u': poster.get('url'),
        'b': _compress_base64_preview(poster.get('base64')),
        't': poster.get('target_type'),
        'g': poster.get('group_id') if poster.get('group_id') != group_id else None,
        'sid': poster.get('set_id'),
        'spc': poster.get('set_poster_count'),
        'up': poster.get('uploader') if poster.get('uploader') != 'Unknown' else None,
        'pid': poster.get('tpdb_poster_id'),
        'src': poster.get('source_url'),
        'sn': poster.get('season_number'),
        'st': poster.get('season_title'),
        'si': poster.get('season_id'),
        'hp': True if poster.get('season_has_poster') else None,
    }
    return {key: value for key, value in compact.items() if value not in (None, '', [], {})}


def _hydrate_tpdb_poster(poster, group_id=None):
    hydrated = {
        'id': poster.get('i'),
        'url': poster.get('u'),
        'base64': poster.get('b'),
        'title': 'Poster',
        'uploader': poster.get('up') or 'Unknown',
        'likes': 0,
        'target_type': poster.get('t'),
        'group_id': poster.get('g') or group_id,
        'set_id': poster.get('sid'),
        'set_url': _build_tpdb_set_url(poster.get('sid')),
        'set_poster_count': poster.get('spc'),
        'tpdb_poster_id': poster.get('pid'),
        'source_url': poster.get('src'),
    }
    if poster.get('si'):
        season_number = poster.get('sn')
        hydrated.update({
            'season_id': poster.get('si'),
            'season_number': season_number,
            'season_title': poster.get('st'),
            'is_special': season_number == 0,
            'season_has_poster': bool(poster.get('hp')),
        })
    return hydrated


def _compact_available_set(set_info):
    compact = {
        'i': str(set_info.get('set_id') or ''),
        'c': set_info.get('set_poster_count'),
        'u': set_info.get('uploader') if set_info.get('uploader') != 'Unknown' else None,
        'p': _compress_base64_preview(set_info.get('preview_base64')),
    }
    return {key: value for key, value in compact.items() if value not in (None, '', [], {})}


def _hydrate_available_set(set_info):
    set_id = set_info.get('i')
    return {
        'set_id': set_id,
        'set_url': _build_tpdb_set_url(set_id),
        'set_poster_count': set_info.get('c'),
        'uploader': set_info.get('u') or 'Unknown',
        'preview_base64': set_info.get('p'),
    }


def _compact_eligible_season(season):
    compact = {
        'i': season.get('id'),
        't': season.get('title'),
        'n': season.get('number'),
        'h': True if season.get('has_poster') else None,
        'u': season.get('thumbnail_url'),
    }
    return {key: value for key, value in compact.items() if value not in (None, '', [], {})}


def _hydrate_eligible_season(season):
    number = season.get('n')
    return {
        'id': season.get('i'),
        'title': season.get('t') or ('Specials' if number == 0 else f"Season {number}"),
        'number': number,
        'is_special': number == 0,
        'has_poster': bool(season.get('h')),
        'thumbnail_url': season.get('u'),
    }


def _read_protected_item_ids():
    path = _get_protected_items_path()
    if not os.path.exists(path):
        return set()

    with open(path, 'r', encoding='utf-8') as protected_file:
        data = json.load(protected_file)

    if isinstance(data, dict):
        items = data.get('items', [])
    else:
        items = data

    return {str(item_id) for item_id in items if item_id}


def _write_protected_item_ids(item_ids):
    _ensure_parent_dir(_get_protected_items_path())
    payload = {
        'updated_at': _utc_timestamp(),
        'items': sorted(str(item_id) for item_id in item_ids if item_id),
    }
    with open(_get_protected_items_path(), 'w', encoding='utf-8') as protected_file:
        json.dump(payload, protected_file, ensure_ascii=False, indent=2)


def _set_item_protected(item_id, protected):
    protected_ids = _read_protected_item_ids()
    item_id = str(item_id)
    if protected:
        protected_ids.add(item_id)
    else:
        protected_ids.discard(item_id)
    _write_protected_item_ids(protected_ids)
    return protected_ids


def _write_failed_log_entry(entry):
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    with open(_get_failed_log_path(), 'a', encoding='utf-8') as failed_log:
        failed_log.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _write_results_log_entry(entry):
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    with open(_get_results_log_path(), 'a', encoding='utf-8') as results_log:
        results_log.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _log_processed_item(item=None, operation='auto-poster', poster_url=None, item_id=None, item_title=None, item_type=None, item_year=None, poster_targets=None, season_results=None):
    """Append one structured successful poster application entry to results.log."""
    try:
        resolved_item = item
        resolved_item_id = item_id or (item or {}).get('id')
        resolved_item_title = item_title or (item or {}).get('title') or 'Unknown'
        resolved_item_type = item_type or (item or {}).get('type')
        resolved_item_year = item_year if item_year is not None else (item or {}).get('year')
        entry = {
            'id': str(uuid.uuid4()),
            'timestamp': _utc_timestamp(),
            'status': 'success',
            'operation': operation,
            'item_id': resolved_item_id,
            'item_title': resolved_item_title,
            'item_type': resolved_item_type,
            'item_year': resolved_item_year,
            'poster_url': poster_url,
            'poster_targets': poster_targets or {},
            'season_results': season_results or [],
        }
        _write_results_log_entry(entry)
        _log_resolved_item(
            resolved_item,
            operation=operation,
            poster_url=poster_url,
            item_id=resolved_item_id,
            item_title=resolved_item_title,
            item_type=resolved_item_type,
            item_year=resolved_item_year,
        )
    except Exception as results_log_error:
        logging.warning(f"Failed to write processed item log: {results_log_error}")


def _read_processed_item_ids():
    results_log_path = _get_results_log_path()
    if not os.path.exists(results_log_path):
        return set()

    processed_item_ids = set()
    with open(results_log_path, 'r', encoding='utf-8') as results_log:
        for line in results_log:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('status') == 'success' and entry.get('item_id'):
                processed_item_ids.add(entry['item_id'])

    return processed_item_ids


def _read_processed_items(limit=500):
    results_log_path = _get_results_log_path()
    if not os.path.exists(results_log_path):
        return []

    entries = []
    with open(results_log_path, 'r', encoding='utf-8') as results_log:
        for line in results_log:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('status') == 'success':
                entries.append(entry)

    latest_entries = []
    seen_item_ids = set()
    for entry in reversed(entries):
        item_id = entry.get('item_id')
        if not item_id or item_id in seen_item_ids:
            continue
        seen_item_ids.add(item_id)
        latest_entries.append(entry)
        if len(latest_entries) >= limit:
            break

    return latest_entries


def _build_processed_history_job(limit=100):
    entries = _read_processed_items(limit=limit)
    if not entries:
        return None

    results = []
    for entry in entries:
        item_title = entry.get('item_title') or entry.get('item_id') or 'Unknown'
        item_year = entry.get('item_year')
        if item_year:
            item_title = f"{item_title} ({item_year})"
        results.append({
            'item_id': entry.get('item_id'),
            'item_title': item_title,
            'success': True,
            'error': None,
            'poster_url': entry.get('poster_url'),
            'season_results': entry.get('season_results') or [],
            'operation': entry.get('operation'),
            'timestamp': entry.get('timestamp'),
        })

    return {
        'job_id': 'processed-history',
        'status': 'completed',
        'phase': 'history',
        'message': f'Showing {len(results)} processed item(s) from results.log.',
        'total_items': len(results),
        'processed': len(results),
        'remaining': 0,
        'successful': len(results),
        'failed': 0,
        'results': results,
        'done': True,
        'success': True,
        'error': None,
        'created_at': entries[-1].get('timestamp'),
        'updated_at': entries[0].get('timestamp'),
        'source': 'results_log',
    }


def _clear_processed_items(item_ids=None):
    results_log_path = _get_results_log_path()
    os.makedirs(os.path.dirname(results_log_path), exist_ok=True)

    if not item_ids:
        with open(results_log_path, 'w', encoding='utf-8'):
            pass
        return 0

    item_ids = set(str(item_id) for item_id in item_ids if item_id)
    if not item_ids:
        return 0
    if not os.path.exists(results_log_path):
        return 0

    kept_lines = []
    removed_count = 0
    with open(results_log_path, 'r', encoding='utf-8') as results_log:
        for line in results_log:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                kept_lines.append(line)
                continue

            if entry.get('status') == 'success' and entry.get('item_id') in item_ids:
                removed_count += 1
            else:
                kept_lines.append(line)

    with open(results_log_path, 'w', encoding='utf-8') as results_log:
        results_log.writelines(kept_lines)

    return removed_count


def _log_failed_item(item=None, error=None, operation='poster', poster_url=None, item_id=None, item_title=None, item_type=None, item_year=None):
    """Append one structured active failure entry to failed.log."""
    try:
        entry = {
            'id': str(uuid.uuid4()),
            'timestamp': _utc_timestamp(),
            'status': 'failed',
            'operation': operation,
            'item_id': item_id or (item or {}).get('id'),
            'item_title': item_title or (item or {}).get('title') or 'Unknown',
            'item_type': item_type or (item or {}).get('type'),
            'item_year': item_year if item_year is not None else (item or {}).get('year'),
            'error': str(error or 'Unknown failure'),
            'poster_url': poster_url,
        }
        _write_failed_log_entry(entry)
    except Exception as failed_log_error:
        logging.warning(f"Failed to write failed item log: {failed_log_error}")


def _log_resolved_item(item=None, operation='retry-auto-poster', poster_url=None, item_id=None, item_title=None, item_type=None, item_year=None):
    """Append a resolution marker so old failures stay in the log but leave the UI."""
    try:
        entry = {
            'id': str(uuid.uuid4()),
            'timestamp': _utc_timestamp(),
            'status': 'resolved',
            'operation': operation,
            'item_id': item_id or (item or {}).get('id'),
            'item_title': item_title or (item or {}).get('title') or 'Unknown',
            'item_type': item_type or (item or {}).get('type'),
            'item_year': item_year if item_year is not None else (item or {}).get('year'),
            'error': None,
            'poster_url': poster_url,
        }
        _write_failed_log_entry(entry)
    except Exception as failed_log_error:
        logging.warning(f"Failed to write resolved item log: {failed_log_error}")


def _read_failed_items(limit=100):
    failed_log_path = _get_failed_log_path()
    if not os.path.exists(failed_log_path):
        return []

    entries = []
    with open(failed_log_path, 'r', encoding='utf-8') as failed_log:
        for line in failed_log:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({
                    'id': str(uuid.uuid4()),
                    'timestamp': None,
                    'status': 'failed',
                    'operation': 'poster',
                    'item_id': None,
                    'item_title': 'Unknown',
                    'item_type': None,
                    'item_year': None,
                    'error': line,
                    'poster_url': None,
                })

    latest_entries = []
    seen_item_ids = set()
    for entry in reversed(entries):
        item_id = entry.get('item_id')
        if item_id:
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            if entry.get('status', 'failed') != 'failed':
                continue
        elif entry.get('status', 'failed') != 'failed':
            continue

        latest_entries.append(entry)
        if len(latest_entries) >= limit:
            break

    return latest_entries


def _find_jellyfin_item(item_id):
    if not item_id:
        return None

    session_id = session.get('session_id')
    session_items = user_sessions.get(session_id, {}).get('items', [])
    item = next((current_item for current_item in session_items if current_item.get('id') == item_id), None)
    if item:
        return item

    return next((current_item for current_item in get_jellyfin_items() if current_item.get('id') == item_id), None)


def _safe_filename_part(value):
    return "".join(c for c in (value or "item") if c.isalnum() or c in " _-").rstrip() or "item"


def _upload_poster_url_to_jellyfin_item(target_id, poster_url, filename_prefix, title):
    safe_title = _safe_filename_part(title)
    save_path = os.path.join(Config.TEMP_POSTER_DIR, f"{filename_prefix}_{safe_title}_{target_id}.jpg")
    if not download_image_with_cookies(poster_url, save_path):
        return False
    try:
        return upload_image_to_jellyfin_improved(target_id, save_path)
    finally:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
        except Exception as cleanup_error:
            logging.warning(f"Failed to cleanup temp file {save_path}: {cleanup_error}")


def _normalize_selection(selection):
    if isinstance(selection, str):
        return {
            'type': 'single',
            'series_poster_url': selection,
            'season_posters': {},
        }
    if isinstance(selection, dict):
        return {
            'type': selection.get('type') or 'series_group',
            'series_poster_url': selection.get('series_poster_url') or selection.get('poster_url'),
            'season_posters': selection.get('season_posters') or {},
        }
    return {'type': 'single', 'series_poster_url': None, 'season_posters': {}}


def _upload_selection_to_jellyfin(item, selection, operation='manual-upload'):
    normalized = _normalize_selection(selection)
    item_id = item.get('id')
    item_title = item.get('title', 'Unknown')
    primary_url = normalized.get('series_poster_url')
    season_posters = normalized.get('season_posters') or {}
    errors = []
    season_results = []
    uploaded_any = False
    series_poster_uploaded = False

    os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
    _sweep_stale_temp_posters()

    if primary_url:
        logging.info(f"Uploading poster to Jellyfin for {item_title}")
        if _upload_poster_url_to_jellyfin_item(item_id, primary_url, operation, item_title):
            uploaded_any = True
            series_poster_uploaded = True
        else:
            errors.append('Failed to upload series poster')
            _log_failed_item(item, 'Failed to upload series poster', operation=operation, poster_url=primary_url)

    for season_id, season_selection in season_posters.items():
        season_url = season_selection.get('url') if isinstance(season_selection, dict) else season_selection
        season_title = season_selection.get('title') if isinstance(season_selection, dict) else f"Season {season_id}"
        if not season_id or not season_url:
            continue
        if _upload_poster_url_to_jellyfin_item(season_id, season_url, operation, f"{item_title}_{season_title}"):
            uploaded_any = True
            season_results.append({'season_id': season_id, 'season_title': season_title, 'success': True, 'poster_url': season_url})
        else:
            error = f"Failed to upload {season_title}"
            errors.append(error)
            season_results.append({'season_id': season_id, 'season_title': season_title, 'success': False, 'poster_url': season_url, 'error': error})

    if uploaded_any:
        successful_seasons = [season for season in season_results if season.get('success')]
        _log_processed_item(
            item,
            operation=operation,
            poster_url=primary_url,
            poster_targets={
                'series_poster': series_poster_uploaded,
                'season_count': len(successful_seasons),
                'season_titles': [season.get('season_title') for season in successful_seasons if season.get('season_title')],
            },
            season_results=season_results,
        )

    return {
        'success': uploaded_any and not errors,
        'uploaded_any': uploaded_any,
        'error': '; '.join(errors) if errors else None,
        'poster_url': primary_url,
        'season_results': season_results,
    }


def _selection_from_poster_group(group, replace_existing_season_posters=False):
    show_posters = group.get('show_posters') or []
    selection = {
        'type': 'series_group',
        'series_poster_url': show_posters[0].get('url') if show_posters else None,
        'season_posters': {},
    }

    for poster in group.get('season_posters') or []:
        season_id = poster.get('season_id')
        if not season_id:
            continue
        if poster.get('season_has_poster') and not replace_existing_season_posters:
            continue
        if season_id in selection['season_posters']:
            continue
        selection['season_posters'][season_id] = {
            'url': poster.get('url'),
            'title': poster.get('season_title') or 'Season',
        }

    return selection


def _auto_fetch_and_upload_item(item, operation='retry-auto-poster'):
    item_id = item['id']
    item_title = item['title']
    posters = search_tpdb_for_posters_multiple(
        item_title,
        item.get('year'),
        item.get('type'),
        tmdb_id=item.get('ProviderIds', {}).get('Tmdb'),
        max_posters=1,
    )

    if not posters:
        error = 'No posters found'
        _log_failed_item(item, error, operation=operation)
        return {
            'item_id': item_id,
            'item_title': item_title,
            'success': False,
            'error': error,
            'poster_url': None,
        }

    poster_url = posters[0]['url']
    os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
    _sweep_stale_temp_posters()
    safe_title = "".join(c for c in item_title if c.isalnum() or c in " _-").rstrip()
    save_path = os.path.join(Config.TEMP_POSTER_DIR, f"retry_{safe_title}_{item_id}.jpg")

    try:
        if not download_image_with_cookies(poster_url, save_path):
            error = 'Failed to download poster'
            _log_failed_item(item, error, operation=operation, poster_url=poster_url)
            return {
                'item_id': item_id,
                'item_title': item_title,
                'success': False,
                'error': error,
                'poster_url': poster_url,
            }

        upload_success = upload_image_to_jellyfin_improved(item_id, save_path)
        if upload_success:
            _log_processed_item(item, operation=operation, poster_url=poster_url)
            return {
                'item_id': item_id,
                'item_title': item_title,
                'success': True,
                'error': None,
                'poster_url': poster_url,
            }

        error = 'Failed to upload to Jellyfin'
        _log_failed_item(item, error, operation=operation, poster_url=poster_url)
        return {
            'item_id': item_id,
            'item_title': item_title,
            'success': False,
            'error': error,
            'poster_url': poster_url,
        }
    finally:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
        except Exception as cleanup_error:
            logging.warning(f"Failed to cleanup temp file {save_path}: {cleanup_error}")

@app.route('/')
def index():
    """Main page showing all Jellyfin items with server info"""
    _evict_stale_user_sessions()

    session_id = session.get('session_id')
    if not session_id:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id

    # Accept 'movies' or 'series' for the client-side visual filter.
    item_type = request.args.get('type', None)
    if item_type not in ('movies', 'series'):
        item_type = None
    current_library = request.args.get('library', None)
    # 'library', 'name', 'year', 'date_added'
    sort_by = request.args.get('sort', 'library')

    try:
        server_info = get_jellyfin_server_info()
        logging.info(f"Connected to server: {server_info['name']}")

        jellyfin_libraries = get_jellyfin_libraries()
        jellyfin_items = get_jellyfin_items(sort_by=sort_by, libraries=jellyfin_libraries)
        library_ids = {library['id'] for library in jellyfin_libraries}
        if current_library not in library_ids:
            current_library = None

        # Store in session
        user_sessions[session_id] = {
            'items': jellyfin_items,
            'selections': {},
            'progress': 0,
            'server_info': server_info,
            'last_seen': time.time()
        }

        return render_template('index.html',
                               items=jellyfin_items,
                               libraries=jellyfin_libraries,
                               server_info=server_info,
                               current_filter=item_type,
                               current_library=current_library,
                               current_sort=sort_by)

    except Exception as e:
        logging.error(f"Error loading main page: {e}")
        return render_template('index.html',
                               items=[],
                               libraries=[],
                               server_info={'name': 'Jellyfin Server', 'version': '', 'id': ''},
                               error=str(e),
                               current_filter=item_type,
                               current_library=current_library,
                               current_sort=sort_by)

@app.route('/item/<item_id>/posters')
def get_item_posters(item_id):
    """Get posters for a specific item"""
    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400
    _touch_session(session_id)

    items = user_sessions[session_id]['items']
    item = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404

    try:
        logging.info(f"Searching posters for: {item['title']}")
        eligible_seasons = get_jellyfin_seasons(item['id']) if item.get('type') == 'Series' else []
        poster_set_limit = request.args.get('set_limit', default=3, type=int)
        poster_set_limit = max(1, min(poster_set_limit or 3, Config.MAX_POSTERS_PER_ITEM))
        requested_set_url = request.args.get('set_url')
        override_tpdb_url = _normalize_tpdb_item_url(request.args.get('tpdb_url'))
        use_cache = request.args.get('use_cache', 'true').lower() != 'false'
        cache_only = request.args.get('cache_only', 'false').lower() == 'true'
        mapped_tpdb_url = override_tpdb_url or (_get_tpdb_item_map_url(item_id) if use_cache else '')
        cache_key = None
        if use_cache and not requested_set_url and not override_tpdb_url and mapped_tpdb_url:
            cache_key = _tpdb_picker_cache_key(item, mapped_tpdb_url, poster_set_limit, eligible_seasons)
            cached_response = _get_cached_tpdb_picker_response(cache_key)
            if cached_response:
                cached_response['item'] = item
                cached_response['from_cache'] = True
                return jsonify(cached_response)

        if cache_only:
            return jsonify({'cache_miss': True, 'from_cache': False})

        if not selenium_ready_event.wait(timeout=30):
            logging.error("Selenium not ready in time for /item/<item_id>/posters")
            return jsonify({'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

        search_result = search_tpdb_for_poster_groups(
            item['title'],
            item_year=item.get('year'),
            item_type=item.get('type'),
            tmdb_id=item.get('ProviderIds', {}).get('Tmdb'),
            eligible_seasons=eligible_seasons,
            max_posters=poster_set_limit if item.get('type') == 'Series' else Config.MAX_POSTERS_PER_ITEM,
            requested_set_urls=[requested_set_url] if requested_set_url else None,
            tpdb_item_url=mapped_tpdb_url,
            cached_available_sets=[],
            preview_max_size=None,
        )
        best_group = search_result.get('best_group') or {}
        resolved_tpdb_url = _set_tpdb_item_map_url(item_id, override_tpdb_url or best_group.get('url') or mapped_tpdb_url)
        if not requested_set_url:
            _cache_tpdb_sets_from_groups(search_result.get('groups', []))
        response_payload = {
            'item': item,
            'posters': search_result.get('posters', []),
            'poster_groups': search_result.get('groups', []),
            'eligible_seasons': eligible_seasons,
            'poster_set_limit': poster_set_limit,
            'can_browse_more_sets': item.get('type') == 'Series' and poster_set_limit < Config.MAX_POSTERS_PER_ITEM,
            'tpdb_mapping_url': resolved_tpdb_url,
            'from_cache': False,
        }
        if use_cache and not requested_set_url and resolved_tpdb_url and not cache_key:
            cache_key = _tpdb_picker_cache_key(item, resolved_tpdb_url, poster_set_limit, eligible_seasons)
        if cache_key:
            _cache_tpdb_picker_response(cache_key, response_payload)
        return jsonify(response_payload)
    except TPDBRateLimited as e:
        logging.warning(f"TPDb challenge/rate-limit for {item_id}: {e}")
        return jsonify({'error': str(e), 'error_type': 'tpdb_rate_limited'}), 429
    except Exception as e:
        logging.error(f"Error getting posters for {item_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/item/<item_id>/season-count')
def get_item_season_count(item_id):
    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400
    _touch_session(session_id)

    item = next((i for i in user_sessions[session_id]['items'] if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    if item.get('type') != 'Series':
        return jsonify({'season_count': None})

    try:
        with season_count_cache_lock:
            cached_count = season_count_cache.get(item_id)
        if cached_count is not None:
            return jsonify({'season_count': cached_count})

        seasons = get_jellyfin_seasons(item_id)
        season_count = len(seasons)
        with season_count_cache_lock:
            _prune_season_count_cache()
            season_count_cache[item_id] = season_count
        return jsonify({'season_count': season_count})
    except Exception as e:
        logging.warning(f"Could not get season count for {item.get('title', item_id)}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/item/<item_id>/seasons')
def get_item_seasons(item_id):
    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400
    _touch_session(session_id)

    item = next((i for i in user_sessions[session_id]['items'] if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    if item.get('type') != 'Series':
        return jsonify({'item': item, 'seasons': []})

    try:
        return jsonify({
            'item': item,
            'seasons': get_jellyfin_seasons(item_id),
        })
    except Exception as e:
        logging.warning(f"Could not get seasons for {item.get('title', item_id)}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/item/<item_id>/select', methods=['POST'])
def select_poster(item_id):
    """User selects a poster for an item (no upload yet)."""
    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400
    _touch_session(session_id)

    data = request.get_json() or {}
    poster_url = data.get('poster_url')
    selection = data.get('selection')
    clear_selection = bool(data.get('clear_selection'))
    if clear_selection:
        user_sessions[session_id]['selections'].pop(item_id, None)
        logging.info(f"Poster selection cleared for item {item_id}")
        return jsonify({'success': True, 'cleared': True})

    if not poster_url and not selection:
        return jsonify({'error': 'No poster selection provided'}), 400

    user_sessions[session_id]['selections'][item_id] = selection or poster_url
    logging.debug(f"Poster selected for item {item_id}")

    return jsonify({'success': True})

@app.route('/upload/<item_id>', methods=['POST'])
def upload_poster(item_id):
    """Upload selected poster to Jellyfin (manual per item)."""
    if not selenium_ready_event.wait(timeout=30):
        logging.error("Selenium not ready in time for /upload/<item_id>")
        return jsonify({'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400
    _touch_session(session_id)

    selections = user_sessions[session_id]['selections']
    if item_id not in selections:
        return jsonify({'error': 'No poster selected for this item'}), 400

    selection = selections[item_id]

    items = user_sessions[session_id]['items']
    item = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404

    try:
        result = _upload_selection_to_jellyfin(item, selection, operation='manual-upload')
        if result['success']:
            return jsonify(result)

        error = result.get('error') or 'Failed to upload to Jellyfin'
        return jsonify({'error': error, **result}), 500

    except Exception as e:
        logging.error(f"Error uploading poster for {item_id}: {e}")
        _log_failed_item(item, e, operation='manual-upload', item_id=item_id)
        return jsonify({'error': str(e)}), 500

@app.route('/upload-all', methods=['POST'])
def upload_all_selected():
    """Upload all selected posters for current session."""
    if not selenium_ready_event.wait(timeout=30):
        logging.error("Selenium not ready in time for /upload-all")
        return jsonify({'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400
    _touch_session(session_id)

    selections = user_sessions[session_id]['selections']
    items = user_sessions[session_id]['items']
    results = []

    logging.info(f"Starting batch upload of {len(selections)} items")
    os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
    _sweep_stale_temp_posters()

    for item_id, selection in selections.items():
        item = None
        try:
            item = next((i for i in items if i['id'] == item_id), None)
            if not item:
                _log_failed_item(error='Item not found', operation='batch-upload', item_id=item_id)
                results.append({'item_id': item_id, 'success': False, 'error': 'Item not found'})
                continue

            upload_result = _upload_selection_to_jellyfin(item, selection, operation='batch-upload')
            results.append({
                'item_id': item_id,
                'item_title': item['title'],
                'success': upload_result['success'],
                'error': upload_result.get('error'),
                'poster_url': upload_result.get('poster_url'),
                'season_results': upload_result.get('season_results', []),
            })

        except Exception as e:
            _log_failed_item(item, e, operation='batch-upload', item_id=item_id)
            results.append({
                'item_id': item_id,
                'item_title': item.get('title', 'Unknown') if item else 'Unknown',
                'success': False,
                'error': str(e)
            })

    return jsonify({'results': results})

@app.route('/jellyfin-image')
def get_jellyfin_image():
    """Proxy endpoint for Jellyfin images with authentication"""
    image_url = request.args.get('url')
    if not image_url:
        return create_placeholder_thumbnail(), 200

    try:
        headers = {
            "X-Emby-Token": Config.JELLYFIN_API_KEY,
            "User-Agent": "Jellyfin-Poster-Manager/1.0",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
        }
        response = requests.get(image_url, headers=headers, timeout=10)
        response.raise_for_status()

        return Response(
            response.content,
            mimetype=response.headers.get('content-type', 'image/jpeg'),
            headers={
                'Cache-Control': 'public, max-age=86400',
                'Access-Control-Allow-Origin': '*',
                'Content-Length': str(len(response.content)),
                'ETag': f'"{hash(image_url)}"'
            }
        )

    except Exception as e:
        logging.warning(f"Error fetching Jellyfin image {image_url}: {e}")
        return create_placeholder_thumbnail(), 200

@app.route('/thumbnail')
def get_thumbnail():
    """Serve TPDb thumbnails with proper headers and caching"""
    thumbnail_url = request.args.get('url')
    if not thumbnail_url or thumbnail_url == 'None':
        return create_placeholder_thumbnail(), 200

    try:
        with requests.Session() as session_obj:
            session_obj.cookies.update(get_selenium_cookies_as_dict())
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Referer": "https://theposterdb.com/",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            }
            response = session_obj.get(thumbnail_url, headers=headers, timeout=10)
            response.raise_for_status()

        return Response(
            response.content,
            mimetype=response.headers.get('content-type', 'image/jpeg'),
            headers={
                'Cache-Control': 'public, max-age=86400',
                'Access-Control-Allow-Origin': '*',
                'Content-Length': str(len(response.content)),
                'ETag': f'"{hash(thumbnail_url)}"'
            }
        )

    except Exception as e:
        logging.warning(f"Error fetching TPDb thumbnail {thumbnail_url}: {e}")
        return create_placeholder_thumbnail(), 200

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        server_info = get_jellyfin_server_info()
        jellyfin_status = "connected" if server_info['name'] != 'Jellyfin Server' else "disconnected"

        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'jellyfin_status': jellyfin_status,
            'server_name': server_info['name'],
            'server_version': server_info.get('version', 'Unknown'),
            'selenium_active': selenium_driver is not None,
            'active_sessions': len(user_sessions)
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'timestamp': datetime.now().isoformat(),
            'error': str(e),
            'selenium_active': selenium_driver is not None,
            'active_sessions': len(user_sessions)
        }), 500

@app.route('/debug/tpdb-search')
def debug_tpdb_search():
    """
    Debug endpoint for TPDb scraping without depending on a Jellyfin item.
    Enabled only in DEBUG mode.
    """
    if not Config.DEBUG:
        return jsonify({'error': 'Not found'}), 404

    if not selenium_ready_event.wait(timeout=30):
        return jsonify({'error': 'Selenium is not ready'}), 503

    title = (request.args.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Missing required query param: title'}), 400

    item_type = request.args.get('type')  # Optional: Movie or Series
    year = request.args.get('year', type=int)
    tmdb_id = request.args.get('tmdb_id')
    max_posters = request.args.get('max_posters', default=3, type=int)
    max_posters = max(1, min(max_posters, 18))

    try:
        posters = search_tpdb_for_posters_multiple(
            item_title=title,
            item_year=year,
            item_type=item_type,
            tmdb_id=tmdb_id,
            max_posters=max_posters,
        )
        return jsonify({
            'success': True,
            'title': title,
            'item_type': item_type,
            'year': year,
            'tmdb_id': tmdb_id,
            'selenium_url': _get_selenium_current_url(),
            'poster_count': len(posters),
            'posters': posters,
        })
    except TPDBRateLimited as e:
        return jsonify({
            'success': False,
            'error_type': 'tpdb_rate_limited',
            'error': str(e),
            'selenium_url': _get_selenium_current_url(),
        }), 429
    except Exception as e:
        logging.exception("Error in /debug/tpdb-search")
        return jsonify({
            'success': False,
            'error_type': 'tpdb_debug_error',
            'error': str(e),
            'selenium_url': _get_selenium_current_url(),
        }), 500


def _select_auto_batch_target_items(all_items, target_filter, skip_processed=False, library_id='', item_ids=None):
    item_id_set = set(str(item_id) for item_id in (item_ids or []) if item_id)
    if target_filter == 'queued':
        target_items = [item for item in all_items if item.get('id') in item_id_set]
    elif target_filter == 'all':
        target_items = all_items
    elif target_filter == 'no-poster':
        target_items = [item for item in all_items if not item.get('thumbnail_url')]
    elif target_filter == 'movies':
        target_items = [item for item in all_items if item.get('type') == 'Movie']
    elif target_filter == 'series':
        target_items = [item for item in all_items if item.get('type') == 'Series']
    else:
        target_items = []

    if library_id:
        target_items = [item for item in target_items if item.get('library_id') == library_id]

    if skip_processed:
        processed_item_ids = _read_processed_item_ids()
        target_items = [item for item in target_items if item.get('id') not in processed_item_ids]

    protected_item_ids = _read_protected_item_ids()
    if protected_item_ids:
        target_items = [item for item in target_items if item.get('id') not in protected_item_ids]

    return target_items


def _auto_search_and_upload_item(item, include_season_posters=False, replace_existing_season_posters=False):
    item_id = item.get('id')
    item_title = item.get('title', 'Unknown')
    item_type = item.get('type')
    old_poster_url = item.get('thumbnail_url')

    if include_season_posters and item_type == 'Series':
        eligible_seasons = get_jellyfin_seasons(item_id)
        search_result = search_tpdb_for_poster_groups(
            item_title,
            item_year=item.get('year'),
            item_type=item_type,
            tmdb_id=item.get('ProviderIds', {}).get('Tmdb'),
            eligible_seasons=eligible_seasons,
            max_posters=1,
            include_base64=False,
        )
        group = search_result.get('best_group')
        if not group:
            raise ValueError('No posters found')

        selection = _selection_from_poster_group(
            group,
            replace_existing_season_posters=replace_existing_season_posters,
        )
        if not selection.get('series_poster_url') and not selection.get('season_posters'):
            raise ValueError('No eligible posters found')

        upload_result = _upload_selection_to_jellyfin(item, selection, operation='auto-poster')
        return {
            'item_id': item_id,
            'item_title': item_title,
            'success': upload_result['success'],
            'error': upload_result.get('error'),
            'old_poster_url': old_poster_url,
            'poster_url': upload_result.get('poster_url'),
            'season_results': upload_result.get('season_results', []),
            'season_posters_uploaded': len([season for season in upload_result.get('season_results', []) if season.get('success')]),
        }

    posters = search_tpdb_for_posters_multiple(
        item_title,
        item.get('year'),
        item_type,
        tmdb_id=item.get('ProviderIds', {}).get('Tmdb'),
        max_posters=1,
    )
    if not posters:
        raise ValueError('No posters found')

    poster_url = posters[0]['url']
    result = _upload_selection_to_jellyfin(item, poster_url, operation='auto-poster')
    return {
        'item_id': item_id,
        'item_title': item_title,
        'success': result['success'],
        'error': result.get('error'),
        'old_poster_url': old_poster_url,
        'poster_url': poster_url,
        'season_results': [],
        'season_posters_uploaded': 0,
    }


def _run_auto_batch_job(job_id, target_filter, skip_processed=False, library_id='', include_season_posters=False, replace_existing_season_posters=False, item_ids=None):
    results = []
    successful_count = 0
    failed_count = 0

    try:
        _update_auto_batch_job(job_id, status='running', phase='preparing', message='Preparing TPDb login...')
        try:
            if not selenium_driver:
                setup_selenium_and_login()
            logging.info("Selenium/TPDb login ready for auto-batch job.")
        except Exception as e:
            logging.error(f"Failed to setup Selenium/login to TPDb: {e}")
            _update_auto_batch_job(
                job_id,
                status='failed',
                phase='failed',
                message='Failed to login to TPDb',
                error=f'Failed to login to TPDb: {str(e)}',
                done=True,
                success=False,
            )
            return

        _update_auto_batch_job(job_id, phase='loading', message='Loading Jellyfin items...')
        all_items = get_jellyfin_items()
        target_items = _select_auto_batch_target_items(
            all_items, target_filter, skip_processed=skip_processed, library_id=library_id, item_ids=item_ids
        )
        total_items = len(target_items)
        _update_auto_batch_job(
            job_id,
            total_items=total_items,
            remaining=total_items,
            message=f'Found {total_items} item(s) to process.',
        )

        if not target_items:
            message = (
                'No unprocessed items found matching the filter criteria.'
                if skip_processed else
                'No items found matching the filter criteria.'
            )
            _update_auto_batch_job(
                job_id,
                status='completed',
                phase='completed',
                message=message,
                results=[],
                done=True,
                success=True,
            )
            return

        logging.info(f"Processing {total_items} items for auto-poster job")
        os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
        _sweep_stale_temp_posters()

        for i, item in enumerate(target_items):
            if _is_auto_batch_cancelled(job_id):
                _finish_auto_batch_cancelled(job_id, results, successful_count, failed_count)
                return

            item_id = item.get('id', 'Unknown')
            item_title = item.get('title', 'Unknown')
            item_year = item.get('year')
            item_type = item.get('type')
            old_poster_url = item.get('thumbnail_url')
            poster_url = None

            try:
                _update_auto_batch_job(
                    job_id,
                    status='running',
                    phase='searching',
                    current_item=item_title,
                    current_item_id=item_id,
                    current_item_type=item_type,
                    current_item_year=item_year,
                    old_poster_url=old_poster_url,
                    new_poster_url=None,
                    processed=i,
                    successful=successful_count,
                    failed=failed_count,
                    message=f'Searching posters for {item_title}...',
                )

                logging.info(f"Processing item {i+1}/{total_items}: {item_title}")
                result = _auto_search_and_upload_item(
                    item,
                    include_season_posters=include_season_posters,
                    replace_existing_season_posters=replace_existing_season_posters,
                )

                if _is_auto_batch_cancelled(job_id):
                    _finish_auto_batch_cancelled(job_id, results, successful_count, failed_count)
                    return

                _update_auto_batch_job(
                    job_id,
                    phase='applying',
                    new_poster_url=result.get('poster_url'),
                    message=f'Applying poster to {item_title}...'
                )

                results.append(result)
                if result.get('success'):
                    successful_count += 1
                    season_count = result.get('season_posters_uploaded', 0)
                    message = f'Applied poster to {item_title}.'
                    if season_count:
                        message = f'Applied poster and {season_count} season poster(s) to {item_title}.'
                    logging.info(f"Successfully uploaded poster for: {item_title}")
                    _update_auto_batch_job(
                        job_id,
                        phase='applied',
                        processed=i + 1,
                        successful=successful_count,
                        results=list(results),
                        message=message,
                    )
                else:
                    failed_count += 1
                    _update_auto_batch_job(
                        job_id,
                        phase='failed',
                        processed=i + 1,
                        failed=failed_count,
                        results=list(results),
                        message=f'Failed to apply poster to {item_title}.',
                    )
            except ValueError as e:
                error_message = str(e)
                result = {
                    'item_id': item_id,
                    'item_title': item_title,
                    'success': False,
                    'error': error_message,
                    'old_poster_url': old_poster_url,
                    'poster_url': None
                }
                _log_failed_item(item, error_message, operation='auto-poster')
                results.append(result)
                failed_count += 1
                _update_auto_batch_job(
                    job_id,
                    phase='failed',
                    processed=i + 1,
                    failed=failed_count,
                    results=list(results),
                    message=f'{error_message} for {item_title}.',
                )
            except TPDBRateLimited as e:
                rate_limited_error = str(e)
                logging.warning(f"TPDb rate-limit detected during batch job; aborting early: {rate_limited_error}")
                result = {
                    'item_id': item_id,
                    'item_title': item_title,
                    'success': False,
                    'error': f'Aborted due to TPDb rate limit: {rate_limited_error}',
                    'old_poster_url': old_poster_url,
                    'poster_url': None
                }
                _log_failed_item(item, result['error'], operation='auto-poster')
                results.append(result)
                failed_count += 1
                _update_auto_batch_job(
                    job_id,
                    status='failed',
                    phase='rate_limited',
                    processed=i + 1,
                    failed=failed_count,
                    results=list(results),
                    message='Batch aborted due to TPDb rate limit.',
                    error=result['error'],
                    done=True,
                    success=False,
                )
                return
            except Exception as e:
                logging.error(f"Error processing item {item_title}: {e}")
                result = {
                    'item_id': item_id,
                    'item_title': item_title,
                    'success': False,
                    'error': str(e),
                    'old_poster_url': old_poster_url,
                    'poster_url': None
                }
                _log_failed_item(item, e, operation='auto-poster')
                results.append(result)
                failed_count += 1
                _update_auto_batch_job(
                    job_id,
                    phase='failed',
                    processed=i + 1,
                    failed=failed_count,
                    results=list(results),
                    message=f'Failed processing {item_title}.',
                )
            finally:
                time.sleep(BATCH_DELAY_SEC)

        _update_auto_batch_job(
            job_id,
            status='completed',
            phase='completed',
            current_item=None,
            processed=len(results),
            successful=successful_count,
            failed=failed_count,
            results=list(results),
            message=f'Batch completed: {successful_count} successful, {failed_count} failed.',
            done=True,
            success=True,
        )
    except Exception as e:
        logging.error(f"Error in auto-batch job: {e}")
        _update_auto_batch_job(
            job_id,
            status='failed',
            phase='failed',
            message='Automatic batch failed.',
            error=str(e),
            results=list(results),
            successful=successful_count,
            failed=failed_count,
            done=True,
            success=False,
        )


@app.route('/batch-auto-poster/start', methods=['POST'])
def start_batch_auto_poster():
    data = request.get_json() or {}
    target_filter = data.get('filter', 'no-poster')
    skip_processed = bool(data.get('skip_processed'))
    library_id = data.get('library_id') or ''
    include_season_posters = bool(data.get('include_season_posters'))
    replace_existing_season_posters = bool(data.get('replace_existing_season_posters'))
    item_ids = data.get('item_ids') if isinstance(data.get('item_ids'), list) else []
    job_id = _create_auto_batch_job(
        target_filter,
        skip_processed=skip_processed,
        include_season_posters=include_season_posters,
        replace_existing_season_posters=replace_existing_season_posters,
        item_ids=item_ids,
    )
    worker = threading.Thread(
        target=_run_auto_batch_job,
        args=(job_id, target_filter, skip_processed, library_id, include_season_posters, replace_existing_season_posters, item_ids),
        daemon=True,
    )
    worker.start()
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/batch-auto-poster/progress/<job_id>')
def batch_auto_poster_progress(job_id):
    job = _get_auto_batch_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Batch job not found'}), 404
    return jsonify({'success': True, 'job': job})


@app.route('/batch-auto-poster/cancel/<job_id>', methods=['POST'])
def cancel_batch_auto_poster(job_id):
    job = _cancel_auto_batch_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Batch job not found'}), 404
    return jsonify({'success': True, 'job': job})


@app.route('/batch-auto-poster/latest-results')
def latest_batch_auto_poster_results():
    job = _get_latest_auto_batch_job()
    history_job = _build_processed_history_job()
    if history_job and (
        not job or not job.get('results') or
        (history_job.get('updated_at') or '') > (job.get('updated_at') or '')
    ):
        job = history_job
    return jsonify({'success': True, 'job': job})


@app.route('/batch-auto-poster', methods=['POST'])
def batch_auto_poster():
    """
    Automatically get and upload the first poster for items based on filter.
    """
    try:
        _evict_stale_user_sessions()

        data = request.get_json() or {}
        target_filter = data.get('filter', 'no-poster')  # 'all', 'no-poster', 'movies', 'series'
        library_id = data.get('library_id') or ''

        logging.info(f"Starting batch auto-poster operation with filter: {target_filter}")

        # Ensure Selenium ready (do not teardown per request)
        try:
            if not selenium_driver:
                setup_selenium_and_login()
            logging.info("Selenium/TPDb login ready for auto-batch.")
        except Exception as e:
            logging.error(f"Failed to setup Selenium/login to TPDb: {e}")
            return jsonify({
                'success': False,
                'error': f'Failed to login to TPDb: {str(e)}',
                'results': [],
                'total_items': 0,
                'processed': 0,
                'successful': 0,
                'failed': 0
            }), 500

        # Get all items
        all_items = get_jellyfin_items()

        target_items = _select_auto_batch_target_items(all_items, target_filter, library_id=library_id)

        if not target_items:
            return jsonify({
                'success': True,
                'message': 'No items found matching the filter criteria',
                'results': [],
                'total_items': 0,
                'processed': 0,
                'successful': 0,
                'failed': 0
            })

        logging.info(f"Processing {len(target_items)} items for auto-poster")

        results = []
        successful_count = 0
        failed_count = 0
        rate_limited_error = None

        os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
        _sweep_stale_temp_posters()

        for i, item in enumerate(target_items):
            try:
                item_id = item['id']
                item_title = item['title']
                item_year = item.get('year')
                item_type = item.get('type')

                logging.info(f"Processing item {i+1}/{len(target_items)}: {item_title}")

                posters = search_tpdb_for_posters_multiple(
                    item_title,
                    item_year,
                    item_type,
                    tmdb_id=item.get('ProviderIds', {}).get('Tmdb'),
                    max_posters=1,
                )

                if not posters:
                    _log_failed_item(item, 'No posters found', operation='auto-poster')
                    results.append({
                        'item_id': item_id,
                        'item_title': item_title,
                        'success': False,
                        'error': 'No posters found',
                        'poster_url': None
                    })
                    failed_count += 1
                    continue

                first_poster = posters[0]
                poster_url = first_poster['url']

                safe_title = "".join(c for c in item_title if c.isalnum() or c in " _-").rstrip()
                save_path = os.path.join(Config.TEMP_POSTER_DIR, f"auto_{safe_title}_{item_id}.jpg")

                if download_image_with_cookies(poster_url, save_path):
                    upload_success = upload_image_to_jellyfin_improved(item_id, save_path)

                    try:
                        if os.path.exists(save_path):
                            os.remove(save_path)
                    except Exception as cleanup_error:
                        logging.warning(f"Failed to cleanup temp file {save_path}: {cleanup_error}")

                    if upload_success:
                        _log_processed_item(item, operation='auto-poster', poster_url=poster_url)
                        results.append({
                            'item_id': item_id,
                            'item_title': item_title,
                            'success': True,
                            'error': None,
                            'poster_url': poster_url
                        })
                        successful_count += 1
                        logging.info(f"Successfully uploaded poster for: {item_title}")
                    else:
                        _log_failed_item(item, 'Failed to upload to Jellyfin', operation='auto-poster', poster_url=poster_url)
                        results.append({
                            'item_id': item_id,
                            'item_title': item_title,
                            'success': False,
                            'error': 'Failed to upload to Jellyfin',
                            'poster_url': poster_url
                        })
                        failed_count += 1
                else:
                    _log_failed_item(item, 'Failed to download poster', operation='auto-poster', poster_url=poster_url)
                    results.append({
                        'item_id': item_id,
                        'item_title': item_title,
                        'success': False,
                        'error': 'Failed to download poster',
                        'poster_url': poster_url
                    })
                    failed_count += 1
            except TPDBRateLimited as e:
                rate_limited_error = str(e)
                logging.warning(f"TPDb rate-limit detected during batch; aborting early: {rate_limited_error}")
                _log_failed_item(item, f'Aborted due to TPDb rate limit: {rate_limited_error}', operation='auto-poster')
                results.append({
                    'item_id': item.get('id', 'Unknown'),
                    'item_title': item.get('title', 'Unknown'),
                    'success': False,
                    'error': f'Aborted due to TPDb rate limit: {rate_limited_error}',
                    'poster_url': None
                })
                failed_count += 1
                break
            except Exception as e:
                logging.error(f"Error processing item {item.get('title', 'Unknown')}: {e}")
                _log_failed_item(item, e, operation='auto-poster')
                results.append({
                    'item_id': item.get('id', 'Unknown'),
                    'item_title': item.get('title', 'Unknown'),
                    'success': False,
                    'error': str(e),
                    'poster_url': None
                })
                failed_count += 1
            finally:
                time.sleep(BATCH_DELAY_SEC)

        if rate_limited_error:
            return jsonify({
                'success': False,
                'error': f'Batch aborted due to TPDb rate limit: {rate_limited_error}',
                'results': results,
                'total_items': len(target_items),
                'processed': len(results),
                'successful': successful_count,
                'failed': failed_count
            }), 429

        logging.info(f"Batch auto-poster completed: {successful_count} successful, {failed_count} failed")

        return jsonify({
            'success': True,
            'message': f'Batch operation completed: {successful_count} successful, {failed_count} failed',
            'results': results,
            'total_items': len(target_items),
            'processed': len(results),
            'successful': successful_count,
            'failed': failed_count
        })

    except Exception as e:
        logging.error(f"Error in batch auto-poster: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'results': [],
            'total_items': 0,
            'processed': 0,
            'successful': 0,
            'failed': 0
        }), 500


@app.route('/failed-items')
def failed_items():
    """Return the most recent failed poster operations from failed.log."""
    limit = request.args.get('limit', default=100, type=int)
    limit = max(1, min(limit, 500))
    try:
        return jsonify({
            'items': _read_failed_items(limit=limit),
            'log_file': _get_failed_log_path(),
        })
    except Exception as e:
        logging.error(f"Error reading failed items log: {e}")
        return jsonify({'items': [], 'error': str(e)}), 500


@app.route('/processed-items')
def processed_items():
    """Return the most recent successful poster applications from results.log."""
    limit = request.args.get('limit', default=500, type=int)
    limit = max(1, min(limit, 1000))
    try:
        return jsonify({
            'items': _read_processed_items(limit=limit),
            'log_file': _get_results_log_path(),
        })
    except Exception as e:
        logging.error(f"Error reading processed items log: {e}")
        return jsonify({'items': [], 'error': str(e)}), 500


@app.route('/processed-items', methods=['DELETE'])
def clear_processed_items():
    """Clear successful poster application history from results.log."""
    try:
        data = request.get_json(silent=True) or {}
        item_ids = data.get('item_ids')
        removed_count = _clear_processed_items(item_ids if isinstance(item_ids, list) else None)
        return jsonify({'success': True, 'removed_count': removed_count})
    except Exception as e:
        logging.error(f"Error clearing processed items log: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/tpdb-cache', methods=['DELETE'])
def clear_tpdb_cache():
    """Clear cached TPDb picker and set preview data without removing item URL mappings."""
    try:
        cleared_files = _clear_tpdb_cache()
        return jsonify({'success': True, 'cleared_files': cleared_files})
    except Exception as e:
        logging.error(f"Error clearing TPDb cache: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/protected-items')
def protected_items():
    """Return items protected from Auto-Get batch processing."""
    try:
        return jsonify({
            'items': sorted(_read_protected_item_ids()),
            'file': _get_protected_items_path(),
        })
    except Exception as e:
        logging.error(f"Error reading protected items: {e}")
        return jsonify({'items': [], 'error': str(e)}), 500


@app.route('/protected-items/toggle', methods=['POST'])
def toggle_protected_item():
    """Protect or unprotect a Jellyfin item from Auto-Get batch processing."""
    data = request.get_json() or {}
    item_id = data.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Missing item_id'}), 400

    try:
        protected_ids = _read_protected_item_ids()
        current_state = str(item_id) in protected_ids
        protected = bool(data.get('protected')) if 'protected' in data else not current_state
        updated_ids = _set_item_protected(item_id, protected)
        return jsonify({
            'success': True,
            'item_id': str(item_id),
            'protected': str(item_id) in updated_ids,
            'items': sorted(updated_ids),
        })
    except Exception as e:
        logging.error(f"Error updating protected item {item_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/failed-items', methods=['DELETE'])
def clear_failed_items():
    """Clear failed.log after the user has reviewed failures."""
    try:
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        with open(_get_failed_log_path(), 'w', encoding='utf-8'):
            pass
        return jsonify({'success': True, 'items': []})
    except Exception as e:
        logging.error(f"Error clearing failed items log: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/failed-items/retry', methods=['POST'])
def retry_failed_item():
    """Retry auto-fetching and uploading a poster for one failed item."""
    if not selenium_ready_event.wait(timeout=30):
        logging.error("Selenium not ready in time for /failed-items/retry")
        return jsonify({'success': False, 'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

    data = request.get_json() or {}
    item_id = data.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Missing item_id'}), 400

    try:
        item = _find_jellyfin_item(item_id)
        if not item:
            _log_failed_item(error='Item not found', operation='retry-auto-poster', item_id=item_id)
            return jsonify({'success': False, 'error': 'Item not found', 'item_id': item_id}), 404

        result = _auto_fetch_and_upload_item(item, operation='retry-auto-poster')
        if result['success']:
            _log_resolved_item(item, operation='retry-auto-poster', poster_url=result.get('poster_url'))
        return jsonify(result), 200 if result['success'] else 500
    except TPDBRateLimited as e:
        _log_failed_item(error=e, operation='retry-auto-poster', item_id=item_id)
        return jsonify({'success': False, 'error': str(e), 'item_id': item_id}), 429
    except Exception as e:
        logging.error(f"Error retrying failed item {item_id}: {e}")
        _log_failed_item(error=e, operation='retry-auto-poster', item_id=item_id)
        return jsonify({'success': False, 'error': str(e), 'item_id': item_id}), 500


@app.route('/failed-items/retry-all', methods=['POST'])
def retry_all_failed_items():
    """Retry auto-fetching and uploading posters for recent failed item IDs."""
    if not selenium_ready_event.wait(timeout=30):
        logging.error("Selenium not ready in time for /failed-items/retry-all")
        return jsonify({'success': False, 'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

    data = request.get_json() or {}
    try:
        limit = int(data.get('limit', 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    failed_entries = _read_failed_items(limit=limit)
    item_ids = []
    for entry in failed_entries:
        item_id = entry.get('item_id')
        if item_id and item_id not in item_ids:
            item_ids.append(item_id)

    results = []
    for item_id in item_ids:
        try:
            item = _find_jellyfin_item(item_id)
            if not item:
                _log_failed_item(error='Item not found', operation='retry-all-auto-poster', item_id=item_id)
                results.append({'item_id': item_id, 'success': False, 'error': 'Item not found'})
                continue

            result = _auto_fetch_and_upload_item(item, operation='retry-all-auto-poster')
            if result.get('success'):
                _log_resolved_item(item, operation='retry-all-auto-poster', poster_url=result.get('poster_url'))
            results.append(result)
        except TPDBRateLimited as e:
            _log_failed_item(error=e, operation='retry-all-auto-poster', item_id=item_id)
            results.append({'item_id': item_id, 'success': False, 'error': str(e)})
            break
        except Exception as e:
            logging.error(f"Error retrying failed item {item_id}: {e}")
            _log_failed_item(error=e, operation='retry-all-auto-poster', item_id=item_id)
            results.append({'item_id': item_id, 'success': False, 'error': str(e)})
        finally:
            time.sleep(BATCH_DELAY_SEC)

    successful_count = len([result for result in results if result.get('success')])
    failed_count = len(results) - successful_count
    return jsonify({
        'success': failed_count == 0,
        'results': results,
        'processed': len(results),
        'successful': successful_count,
        'failed': failed_count,
    })


@app.route('/jellyfin-items')
def jellyfin_items():
    """Get all Jellyfin items using the existing poster_scraper function"""
    try:
        item_type = request.args.get('type')  # 'movies', 'series', or None
        sort_by = request.args.get('sort', 'name')
        items = get_jellyfin_items(item_type=item_type, sort_by=sort_by)
        server_info = get_jellyfin_server_info()
        return jsonify({
            'items': items,
            'server_info': server_info,
            'total_count': len(items)
        })
    except Exception as e:
        logging.error(f"Error fetching Jellyfin items: {e}")
        return jsonify({
            'error': str(e),
            'items': [],
            'server_info': {'name': 'Jellyfin Server', 'version': '', 'id': ''},
            'total_count': 0
        }), 500

@app.route('/upload-poster', methods=['POST'])
def upload_poster_direct():
    """
    Upload a poster directly from URL to Jellyfin.
    This endpoint remains for direct one-off uploads but the UI now uses selection + /upload/<id>.
    """
    try:
        if not selenium_ready_event.wait(timeout=30):
            logging.error("Selenium not ready in time for /upload-poster")
            return jsonify({'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

        _evict_stale_user_sessions()
        data = request.get_json() or {}
        item_id = data.get('item_id')
        poster_url = data.get('poster_url')

        if not item_id or not poster_url:
            return jsonify({'success': False, 'error': 'Missing item_id or poster_url'}), 400

        session_id = session.get('session_id')
        if session_id in user_sessions:
            _touch_session(session_id)
        items = user_sessions.get(session_id, {}).get('items', [])
        item = next((i for i in items if i['id'] == item_id), None)
        if not item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404

        os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
        _sweep_stale_temp_posters()
        save_path = os.path.join(Config.TEMP_POSTER_DIR, f"manual_{item_id}.jpg")

        if download_image_with_cookies(poster_url, save_path):
            upload_success = upload_image_to_jellyfin_improved(item_id, save_path)
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except Exception as cleanup_error:
                logging.warning(f"Failed to cleanup temp file {save_path}: {cleanup_error}")

            if upload_success:
                _log_processed_item(item, operation='direct-upload', poster_url=poster_url)
                return jsonify({'success': True, 'message': 'Poster uploaded successfully'})
            else:
                _log_failed_item(item, 'Failed to upload to Jellyfin', operation='direct-upload', poster_url=poster_url)
                return jsonify({'success': False, 'error': 'Failed to upload to Jellyfin'}), 500
        else:
            _log_failed_item(item, 'Failed to download poster', operation='direct-upload', poster_url=poster_url)
            return jsonify({'success': False, 'error': 'Failed to download poster'}), 500

    except Exception as e:
        logging.error(f"Error uploading poster: {e}")
        _log_failed_item(item if 'item' in locals() else None, e, operation='direct-upload', poster_url=poster_url if 'poster_url' in locals() else None, item_id=item_id if 'item_id' in locals() else None)
        return jsonify({'success': False, 'error': str(e)}), 500

def create_placeholder_thumbnail():
    svg_content = '''
    <svg width="200" height="300" xmlns="http://www.w3.org/2000/svg">
        <rect width="100%" height="100%" fill="#f8f9fa" stroke="#dee2e6" stroke-width="2"/>
        <circle cx="100" cy="120" r="30" fill="#dee2e6"/>
        <rect x="70" y="180" width="60" height="8" fill="#dee2e6" rx="4"/>
        <rect x="80" y="200" width="40" height="6" fill="#dee2e6" rx="3"/>
        <text x="100" y="250" font-family="Arial" font-size="12" fill="#6c757d" text-anchor="middle">
            No Preview
        </text>
    </svg>
    '''
    return Response(svg_content, mimetype='image/svg+xml')

def background_setup():
    try:
        setup_selenium_and_login()

        try:
            server_info = get_jellyfin_server_info()
            logging.info(f"Connected to Jellyfin server: {server_info['name']} (v{server_info.get('version', 'Unknown')})")
        except Exception as e:
            logging.warning(f"Could not connect to Jellyfin server: {e}")

    except Exception as e:
        logging.error(f"Failed to perform background setup: {e}")
    finally:
        # Always release startup waiters. If Selenium login failed, routes can still
        # attempt setup on demand and return a concrete TPDb error instead of permanent 503.
        selenium_ready_event.set()


if __name__ == '__main__':
    host = '0.0.0.0'
    port = int(getattr(Config, 'WEB_PORT', 5001))

    setup_thread = threading.Thread(target=background_setup, daemon=True)
    setup_thread.start()

    try:
        app.run(debug=Config.DEBUG, host=host, port=port)
    except KeyboardInterrupt:
        logging.info("Shutdown requested by CTRL+C")
    except Exception as e:
        logging.error(f"Failed to start Flask application: {e}")
    finally:
        teardown_selenium()
        logging.info("Application shutdown complete")
