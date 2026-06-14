import os

class Config:
    # Flask Configuration
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    DEBUG = False
    
    # Jellyfin Configuration
    JELLYFIN_URL = ""
    JELLYFIN_API_KEY = ""
    
    # TPDb Configuration
    TPDB_BASE_URL = "https://theposterdb.com"
    TPDB_SEARCH_URL_TEMPLATE = "https://theposterdb.com/search?term={query}"
    TPDB_EMAIL = ""
    TPDB_PASSWORD = ""

    # TMDB Configuration
    TMDB_API_KEY = ""

    # Application Settings
    WEB_PORT = 5001
    MAX_POSTERS_PER_ITEM = 18
    TPDB_BATCH_DELAY_SEC = 1.5
    TPDB_DEBUG_SNAPSHOTS = False
    LOG_DIR = "logs"
    CACHE_DIR = "cache"
    APP_STATE_DIR = "data"
    TEMP_POSTER_DIR = os.path.join(CACHE_DIR, "temp_posters")
    FAILED_LOG_FILE = os.path.join(LOG_DIR, "failed.log")
    RESULTS_LOG_FILE = os.path.join(LOG_DIR, "results.log")
    PROTECTED_ITEMS_FILE = os.path.join(APP_STATE_DIR, "protected_items.json")
    TPDB_ITEM_MAP_FILE = os.path.join(APP_STATE_DIR, "tpdb_item_map.json")
    TPDB_SET_CACHE_FILE = os.path.join(CACHE_DIR, "tpdb_set_cache.json")
    TPDB_PICKER_CACHE_FILE = os.path.join(CACHE_DIR, "tpdb_picker_cache.json")
    TPDB_SET_CACHE_MAX_AGE_DAYS = 14
    TPDB_PICKER_CACHE_MAX_AGE_DAYS = 7
