import os

class Config:
    # Flask Configuration
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    DEBUG = True
    
    # Jellyfin Configuration
    JELLYFIN_URL = ""
    JELLYFIN_API_KEY = ""
    
    # TPDB Configuration
    TPDB_BASE_URL = "https://theposterdb.com"
    TPDB_SEARCH_URL_TEMPLATE = "https://theposterdb.com/search?term={query}"
    TPDB_EMAIL = ""
    TPDB_PASSWORD = ""

    # TMDB Configuration
    TMDB_API_KEY = ""

    # Application Settings
    MAX_POSTERS_PER_ITEM = 18
    TEMP_POSTER_DIR = "temp_posters"
    LOG_DIR = "logs"
