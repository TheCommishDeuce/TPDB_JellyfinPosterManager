import requests
import json
from io import BytesIO
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import os
import hashlib
import base64
from urllib.parse import quote_plus
from config import Config
import logging
from requests.exceptions import ChunkedEncodingError, ConnectionError

# Global Selenium driver
selenium_driver = None

def _iter_file_chunks(file_obj, chunk_size=1024 * 1024):
    while True:
        data = file_obj.read(chunk_size)
        if not data:
            break
        yield data

def setup_selenium_and_login():
    """
    Initialize a singleton Selenium driver and log into ThePosterDB.
    Safe to call multiple times; it will reuse the global driver if available.
    """
    global selenium_driver

    if selenium_driver:
        logging.info("Selenium already initialized.")
        return

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    selenium_driver = webdriver.Chrome(options=chrome_options)

    try:
        # Login to TPDB
        selenium_driver.get("https://theposterdb.com/login")
        time.sleep(1.5)

        email_input = selenium_driver.find_element(By.NAME, "login")
        password_input = selenium_driver.find_element(By.NAME, "password")
        email_input.clear()
        email_input.send_keys(Config.TPDB_EMAIL)
        password_input.clear()
        password_input.send_keys(Config.TPDB_PASSWORD)
        password_input.send_keys(Keys.RETURN)
        time.sleep(4)  # Wait for login to complete

        logging.info("Selenium initialized and logged into ThePosterDB.")
    except Exception as e:
        logging.error(f"Failed to login to ThePosterDB: {e}")
        teardown_selenium()
        raise

def teardown_selenium():
    """Shutdown Selenium driver (only used on app shutdown)."""
    global selenium_driver
    if selenium_driver:
        try:
            selenium_driver.quit()
        except Exception:
            pass
        selenium_driver = None

def get_selenium_cookies_as_dict():
    """Return Selenium cookies as a dict for requests.Session."""
    global selenium_driver
    if not selenium_driver:
        return {}
    try:
        cookies = selenium_driver.get_cookies()
        return {cookie['name']: cookie['value'] for cookie in cookies}
    except Exception:
        return {}

def download_image_with_cookies(url, save_path):
    """
    Download an image from TPDB using Selenium cookies for authentication.
    """
    try:
        # Ensure target dir exists
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        session = requests.Session()
        session.cookies.update(get_selenium_cookies_as_dict())
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://theposterdb.com/",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
        })
        response = session.get(url, stream=True, timeout=30)
        if response.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in response.iter_content(8192):
                    if chunk:
                        f.write(chunk)
            logging.info(f"Saved image to {save_path}")
            return True
        else:
            logging.warning(f"Failed to download image from {url} (status {response.status_code})")
            return False
    except Exception as e:
        logging.error(f"Error downloading image from {url}: {e}")
        return False

def get_content_type(file_path):
    ext = file_path.split('.')[-1].lower()
    return {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp'
    }.get(ext, 'application/octet-stream')

def calculate_hash(data):
    return hashlib.md5(data).hexdigest()

def get_local_image_hash(image_path):
    try:
        if not os.path.exists(image_path):
            return None
        with open(image_path, 'rb') as f:
            data = f.read()
            return calculate_hash(data)
    except Exception as e:
        logging.error(f"Error calculating hash for {image_path}: {str(e)}")
        return None

def get_jellyfin_image_hash(item_id, image_type='Primary', index=0):
    try:
        url = f"{Config.JELLYFIN_URL}/Items/{item_id}/Images/{image_type}/{index}"
        headers = {'X-Emby-Token': Config.JELLYFIN_API_KEY}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return calculate_hash(response.content)
    except Exception as e:
        logging.error(f"Error getting image hash from Jellyfin: {str(e)}")
        return None

def are_images_identical(item_id, image_path, image_type='Primary'):
    if not os.path.exists(image_path):
        return False
    jellyfin_hash = get_jellyfin_image_hash(item_id, image_type)
    if not jellyfin_hash:
        return False
    local_hash = get_local_image_hash(image_path)
    if not local_hash:
        return False
    return jellyfin_hash == local_hash

def get_image_as_base64(image_url):
    """
    Download image and convert to base64 data URL for embedding in UI.
    """
    try:
        session = requests.Session()
        session.cookies.update(get_selenium_cookies_as_dict())
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://theposterdb.com/",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
        })

        logging.debug(f"Converting image to base64: {image_url}")
        response = session.get(image_url, timeout=15)
        response.raise_for_status()

        image_data = base64.b64encode(response.content).decode('utf-8')
        content_type = response.headers.get('content-type', 'image/jpeg')
        return f"data:{content_type};base64,{image_data}"
    except Exception as e:
        logging.warning(f"Error converting image to base64: {e}")
        return None

def search_tpdb_for_posters_multiple(item_title, item_year=None, item_type=None, tmdb_id=None, max_posters=18):
    """
    Return up to max_posters poster URLs with base64 data for preview.
    item_type should be "Movie" or "Series" (Jellyfin item Type).
    """
    global selenium_driver
    poster_data = []

    # Determine TMDB media type
    tmdb_type = None
    if item_type == "Movie":
        tmdb_type = "movie"
    elif item_type == "Series":
        tmdb_type = "tv"

    search_query = item_title

    # Prefer TMDB title + year if available
    if tmdb_id and tmdb_type:
        try:
            tmdb_response = requests.get(
                f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={Config.TMDB_API_KEY}&language=en-US",
                timeout=10
            )
            tmdb_response.raise_for_status()
            tmdb_data = tmdb_response.json()

            if tmdb_type == "tv":
                tmdb_title = tmdb_data.get("name")
                year = (tmdb_data.get("first_air_date") or "")[:4]
            else:
                tmdb_title = tmdb_data.get("title")
                year = (tmdb_data.get("release_date") or "")[:4]

            if tmdb_title:
                search_query = f'{tmdb_title} ({year})' if year else tmdb_title
                logging.info(f"Using TMDB title for TPDB search: {search_query}")
        except Exception as e:
            logging.warning(f"TMDB lookup failed for {item_title} ({item_type}): {e}; falling back to Jellyfin title.")

    encoded_query = quote_plus(search_query)
    search_url = Config.TPDB_SEARCH_URL_TEMPLATE.format(query=encoded_query)

    # Optional section narrowing
    if item_type == "Movie":
        search_url += "&section=movies"
    elif item_type == "Series":
        search_url += "&section=shows"

    logging.info(f"TPDB search URL: {search_url}")

    try:
        if not selenium_driver:
            setup_selenium_and_login()

        selenium_driver.get(search_url)
        time.sleep(1.5)
        soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')

        # Search result links
        search_result_links = soup.find_all(
            "a",
            class_="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative"
        )

        if not search_result_links:
            logging.info(f"No TPDB search results for '{search_query}'.")
            return []

        # Best match by simple title similarity
        best_match = None
        best_match_score = 0
        for link in search_result_links:
            try:
                title_element = link.find(class_="text-truncate") or link.find("span") or link
                result_title = title_element.get_text(strip=True) if title_element else link.get_text(strip=True)
                score = calculate_title_match_score(search_query, result_title)
                if score > best_match_score:
                    best_match_score = score
                    best_match = link
            except Exception:
                continue

        selected_link = best_match if best_match and best_match_score >= 0.8 else search_result_links[0]

        # Build item page URL
        item_page_path = selected_link.get('href')
        if not item_page_path:
            return []
        target_item_page_url = item_page_path if item_page_path.startswith('http') else (
            Config.TPDB_BASE_URL + item_page_path if item_page_path.startswith('/') else None
        )
        if not target_item_page_url:
            return []

        # Open item page and extract poster links
        selenium_driver.get(target_item_page_url)
        time.sleep(1.5)
        item_soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')

        poster_links = item_soup.find_all(
            "a",
            class_="bg-transparent border-0 text-white",
            href=True
        )[:max_posters]

        logging.info(f"Found {len(poster_links)} poster links; converting to base64 for preview")

        for i, poster_link in enumerate(poster_links):
            href = poster_link['href']
            if href.startswith('http'):
                poster_url = href
            elif href.startswith('/'):
                poster_url = Config.TPDB_BASE_URL + href
            else:
                continue

            base64_image = get_image_as_base64(poster_url)

            poster_data.append({
                'id': i + 1,
                'url': poster_url,
                'base64': base64_image,
                # Keep fields for future use, but UI won't render them
                'title': 'Poster',
                'uploader': 'Unknown',
                'likes': 0
            })

    except Exception as e:
        logging.error(f"Error during TPDB scraping: {e}")

    return poster_data

def extract_poster_metadata(poster_element):
    try:
        title_elem = poster_element.find('title') or poster_element.get('title', '')
        return {
            'title': title_elem if isinstance(title_elem, str) else 'Poster',
            'uploader': 'Unknown',
            'likes': 0
        }
    except Exception:
        return {'title': 'Poster', 'uploader': 'Unknown', 'likes': 0}

def calculate_title_match_score(expected_title, result_title):
    if not expected_title or not result_title:
        return 0.0

    expected_norm = normalize_title_for_comparison(expected_title)
    result_norm = normalize_title_for_comparison(result_title)

    if expected_norm == result_norm:
        return 1.0
    if expected_norm in result_norm or result_norm in expected_norm:
        return 0.9

    expected_words = set(expected_norm.split())
    result_words = set(result_norm.split())
    if not expected_words or not result_words:
        return 0.0

    common = expected_words.intersection(result_words)
    return len(common) / max(len(expected_words), len(result_words))

def normalize_title_for_comparison(title):
    if not title:
        return ""
    normalized = title.lower().strip()
    char_replacements = {
        '&': 'and',
        '+': 'plus',
        '@': 'at',
        '#': 'number',
        '%': 'percent',
    }
    for char, replacement in char_replacements.items():
        normalized = normalized.replace(char, f' {replacement} ')
    normalized = re.sub(r'[^\w\s]', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()

def upload_image_to_jellyfin_improved(item_id, image_path):
    """Upload image to Jellyfin with improved logic"""
    try:
        if not os.path.exists(image_path):
            print(f"Image file not found: {image_path}")
            return False

        # Check if images are identical
        if are_images_identical(item_id, image_path, 'Primary'):
            print(f"Image for item {item_id} is identical to existing.")
            return True

        # Read and encode the image
        with open(image_path, 'rb') as f:
            image_data = f.read()
        
        encoded_data = base64.b64encode(image_data)

        # Prepare the upload
        url = f"{Config.JELLYFIN_URL}/Items/{item_id}/Images/Primary/0"
        headers = {
            'X-Emby-Token': Config.JELLYFIN_API_KEY,
            'Content-Type': get_content_type(image_path),
            'Connection': 'keep-alive'
        }

        # Send the POST request
        response = requests.post(url, headers=headers, data=encoded_data, timeout=30)

        if response.status_code in [200, 204]:
            print("Artwork uploaded successfully!")
            return True
        else:
            print(f"Failed to upload artwork: {response.status_code}")
            return False

    except Exception as e:
        print(f"Error during image upload: {e}")
        return False
    finally:
        # Clean up memory
        if 'encoded_data' in locals():
            del encoded_data

def get_jellyfin_server_info():
    try:
        url = f"{Config.JELLYFIN_URL}/System/Info"
        headers = {"X-Emby-Token": Config.JELLYFIN_API_KEY}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        return {
            'name': data.get('ServerName', 'Jellyfin Server'),
            'version': data.get('Version', ''),
            'id': data.get('Id', '')
        }
    except Exception as e:
        logging.error(f"Error fetching server info: {e}")
        return {'name': 'Jellyfin Server', 'version': '', 'id': ''}

def get_jellyfin_items(item_type=None, sort_by='name'):
    """
    Fetch a list of movies and TV shows from Jellyfin with thumbnail URLs.
    item_type: 'movies', 'series', or None for both
    sort_by: 'name', 'year', 'date_added'
    """
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY:
        logging.error("Jellyfin configuration is missing.")
        return []

    items = []
    headers = {
        "X-Emby-Token": Config.JELLYFIN_API_KEY,
        "Accept": "application/json",
    }

    sort_params = {
        'name': 'SortName',
        'year': 'ProductionYear,SortName',
        'date_added': 'DateCreated'
    }
    sort_by_param = sort_params.get(sort_by, 'SortName')
    sort_order = 'Descending' if sort_by == 'date_added' else 'Ascending'

    try:
        if sort_by == 'date_added':
            logging.info("Fetching all items for chronological sorting (mixed types).")
            all_items_url = (
                f"{Config.JELLYFIN_URL}/Items"
                f"?IncludeItemTypes=Movie,Series&Recursive=true"
                f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,Type"
                f"&SortBy={sort_by_param}&SortOrder={sort_order}"
            )
            response = requests.get(all_items_url, headers=headers, timeout=15)
            response.raise_for_status()
            all_data = response.json()

            if 'Items' in all_data:
                for item in all_data['Items']:
                    thumbnail_url = None
                    if item.get('ImageTags', {}).get('Primary'):
                        thumbnail_url = (
                            f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary"
                            f"?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
                        )
                    items.append({
                        "id": item.get('Id'),
                        "title": item.get('Name'),
                        "year": item.get('ProductionYear'),
                        "type": "Movie" if item.get('Type') == 'Movie' else "Series",
                        "thumbnail_url": thumbnail_url,
                        "date_created": item.get('DateCreated', ''),
                        'ProviderIds': item.get('ProviderIds', {})
                    })
            # Python-side sort for safety
            from datetime import datetime
            def parse_date(date_str):
                if not date_str:
                    return datetime.min
                try:
                    return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                except Exception:
                    return datetime.min
            items.sort(key=lambda x: parse_date(x['date_created']), reverse=True)

        else:
            # Movies
            if item_type == 'movies' or item_type is None:
                movies_url = (
                    f"{Config.JELLYFIN_URL}/Items"
                    f"?IncludeItemTypes=Movie&Recursive=true"
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated"
                    f"&SortBy={sort_by_param}&SortOrder={sort_order}"
                )
                response = requests.get(movies_url, headers=headers, timeout=15)
                response.raise_for_status()
                movies_data = response.json()
                for item in movies_data.get('Items', []):
                    thumbnail_url = None
                    if item.get('ImageTags', {}).get('Primary'):
                        thumbnail_url = (
                            f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary"
                            f"?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
                        )
                    items.append({
                        "id": item.get('Id'),
                        "title": item.get('Name'),
                        "year": item.get('ProductionYear'),
                        "type": "Movie",
                        "thumbnail_url": thumbnail_url,
                        "date_created": item.get('DateCreated', ''),
                        'ProviderIds': item.get('ProviderIds', {})
                    })

            # Series
            if item_type == 'series' or item_type is None:
                shows_url = (
                    f"{Config.JELLYFIN_URL}/Items"
                    f"?IncludeItemTypes=Series&Recursive=true"
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated"
                    f"&SortBy={sort_by_param}&SortOrder={sort_order}"
                )
                response = requests.get(shows_url, headers=headers, timeout=15)
                response.raise_for_status()
                shows_data = response.json()
                for item in shows_data.get('Items', []):
                    thumbnail_url = None
                    if item.get('ImageTags', {}).get('Primary'):
                        thumbnail_url = (
                            f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary"
                            f"?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
                        )
                    items.append({
                        "id": item.get('Id'),
                        "title": item.get('Name'),
                        "year": item.get('ProductionYear'),
                        "type": "Series",
                        "thumbnail_url": thumbnail_url,
                        "date_created": item.get('DateCreated', ''),
                        'ProviderIds': item.get('ProviderIds', {})
                    })
    except Exception as e:
        logging.error(f"Error fetching items from Jellyfin: {e}")
        return []

    logging.info(f"Total items fetched: {len(items)}")
    return items
