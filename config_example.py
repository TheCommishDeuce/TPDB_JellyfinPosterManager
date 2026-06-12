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
    TEMP_POSTER_DIR = "temp_posters"
    LOG_DIR = "logs"
    FAILED_LOG_FILE = os.path.join(LOG_DIR, "failed.log")
    RESULTS_LOG_FILE = os.path.join(LOG_DIR, "results.log")
    PROTECTED_ITEMS_FILE = os.path.join(LOG_DIR, "protected_items.json")
