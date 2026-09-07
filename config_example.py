import os

class Config:
    # Flask Configuration
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    DEBUG = os.environ.get('DEBUG', '').lower() == 'true'
    
    # Jellyfin Configuration
    JELLYFIN_URL = os.environ.get('JELLYFIN_URL', '')
    JELLYFIN_API_KEY = os.environ.get('JELLYFIN_API_KEY', '')
    
    # TPDb Configuration
    TPDB_BASE_URL = "https://theposterdb.com"
    TPDB_SEARCH_URL_TEMPLATE = "https://theposterdb.com/search?term={query}"
    TPDB_EMAIL = os.environ.get('TPDB_EMAIL', '')
    TPDB_PASSWORD = os.environ.get('TPDB_PASSWORD', '')

    # TMDB Configuration
    TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '')

    # Application Settings
    WEB_HOST = os.environ.get('WEB_HOST', '127.0.0.1')  # Single-user/local by default; do not expose without authentication.
    WEB_PORT = int(os.environ.get('WEB_PORT', '5001'))
    MAX_POSTERS_PER_ITEM = 18
    MAX_TPDB_SETS_PER_ITEM = 30
    TPDB_BATCH_DELAY_SEC = float(os.environ.get('TPDB_BATCH_DELAY_SEC', '1.5'))
    TPDB_DEBUG_SNAPSHOTS = os.environ.get('TPDB_DEBUG_SNAPSHOTS', '').lower() == 'true'
    LOG_DIR = os.environ.get('LOG_DIR', 'logs')
    CACHE_DIR = os.environ.get('CACHE_DIR', 'cache')
    APP_STATE_DIR = os.environ.get('APP_STATE_DIR', 'data')
    TEMP_POSTER_DIR = os.path.join(CACHE_DIR, "temp_posters")
    # These legacy files are imported into APP_STATE_DIR/poster_manager.sqlite3 once.
    # Originals are retained; subsequent state updates go to SQLite.
    FAILED_LOG_FILE = os.path.join(LOG_DIR, "failed.log")
    RESULTS_LOG_FILE = os.path.join(LOG_DIR, "results.log")
    PROTECTED_ITEMS_FILE = os.path.join(APP_STATE_DIR, "protected_items.json")
    TPDB_ITEM_MAP_FILE = os.path.join(APP_STATE_DIR, "tpdb_item_map.json")
    # Disposable picker-v2/*.json responses do not contain embedded image data.
    TPDB_PICKER_CACHE_MAX_AGE_DAYS = int(os.environ.get('TPDB_PICKER_CACHE_MAX_AGE_DAYS', '7'))
