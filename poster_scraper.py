import requests
import json
from io import BytesIO
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import os
import hashlib
import base64
from datetime import datetime
import threading
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from config import Config
import logging
from requests.exceptions import ChunkedEncodingError, ConnectionError

# Global Selenium driver
selenium_driver = None
selenium_lock = threading.RLock()

SEARCH_RESULT_SELECTOR = "a.btn.btn-dark-lighter.flex-grow-1.text-truncate.py-2.text-left.position-relative"
ITEM_POSTER_SELECTOR = "a.bg-transparent.border-0.text-white"
TPDB_PAGE_REQUEST_DELAY_SEC = 1.25
TPDB_IMAGE_PREVIEW_DELAY_SEC = 0.75
TPDB_IMAGE_PREVIEW_RETRY_DELAY_SEC = 3
RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "checking your browser before accessing",
    "attention required",
    "just a moment",
    "verify you are human",
    "cf-chl",
    "managed challenge",
)
RATE_LIMIT_URL_MARKERS = (
    "/cdn-cgi/challenge-platform",
    "challenge-platform",
    "captcha",
    "managed-challenge",
)
RATE_LIMIT_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "one more step",
    "checking your browser",
)


class TPDBSessionExpired(Exception):
    """Raised when TPDB redirects Selenium back to /login."""


class TPDBRateLimited(Exception):
    """Raised when TPDB responds with a likely rate-limit/challenge page."""


def _iter_file_chunks(file_obj, chunk_size=1024 * 1024):
    while True:
        data = file_obj.read(chunk_size)
        if not data:
            break
        yield data


def _is_login_url(url):
    return "/login" in (url or "").lower()


def _is_rate_limit_url(current_url):
    url_lower = (current_url or "").lower()
    return any(marker in url_lower for marker in RATE_LIMIT_URL_MARKERS)


def _extract_html_title(page_source):
    if not page_source:
        return ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page_source, flags=re.IGNORECASE | re.DOTALL)
    if not title_match:
        return ""
    return re.sub(r"\s+", " ", title_match.group(1)).strip()


def _write_tpdb_debug_snapshot(page_source, context_label):
    should_snapshot = getattr(Config, "TPDB_DEBUG_SNAPSHOTS", getattr(Config, "DEBUG", False))
    if not should_snapshot:
        return None
    try:
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        safe_context = re.sub(r"[^a-zA-Z0-9_-]+", "_", (context_label or "tpdb"))
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        snapshot_path = os.path.join(Config.LOG_DIR, f"tpdb_{safe_context}_{timestamp}.html")
        with open(snapshot_path, "w", encoding="utf-8") as snapshot_file:
            snapshot_file.write(page_source or "")
        return snapshot_path
    except Exception as snapshot_error:
        logging.warning(f"Failed to write TPDB debug snapshot: {snapshot_error}")
        return None


def _raise_if_rate_limited(page_source, current_url, context_label="search", check_content_markers=True):
    page_lower = (page_source or "").lower()
    title = (_extract_html_title(page_source) or "unknown title")
    title_lower = title.lower()
    title_looks_challenge = any(marker in title_lower for marker in RATE_LIMIT_TITLE_MARKERS)
    page_looks_challenge = check_content_markers and any(marker in page_lower for marker in RATE_LIMIT_MARKERS)

    if _is_rate_limit_url(current_url) or title_looks_challenge or page_looks_challenge:
        snapshot_path = _write_tpdb_debug_snapshot(page_source, f"{context_label}_challenge")
        details = f"TPDB returned a rate-limit/challenge page at {current_url or 'unknown URL'} (title: {title})"
        if snapshot_path:
            details += f". Snapshot: {snapshot_path}"
        raise TPDBRateLimited(details)


def _wait_for_search_results_ready(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, SEARCH_RESULT_SELECTOR)
            or "no results" in (d.page_source or "").lower()
            or "0 results" in (d.page_source or "").lower()
        )
    except TimeoutException as exc:
        _raise_if_rate_limited(driver.page_source, driver.current_url, "search_timeout")
        raise TimeoutError("Timed out waiting for TPDB search results page to load.") from exc


def _wait_for_item_posters_ready(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, ITEM_POSTER_SELECTOR)
            or "no posters" in (d.page_source or "").lower()
            or "no poster" in (d.page_source or "").lower()
        )
    except TimeoutException as exc:
        _raise_if_rate_limited(driver.page_source, driver.current_url, "item_timeout")
        raise TimeoutError("Timed out waiting for TPDB item poster page to load.") from exc


def _get_selenium_current_url():
    global selenium_driver
    if not selenium_driver:
        return None
    try:
        return selenium_driver.current_url
    except Exception:
        return None


def setup_selenium_and_login(force=False):
    """
    Initialize a singleton Selenium driver and log into ThePosterDB.
    Safe to call multiple times; it will reuse the global driver if available.
    force=True re-authenticates the existing driver session.
    """
    global selenium_driver
    with selenium_lock:
        if selenium_driver and not force:
            logging.info("Selenium already initialized.")
            return

        created_driver = False
        if not selenium_driver:
            chrome_options = Options()
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("--lang=en-US")
            chrome_options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option("useAutomationExtension", False)
            selenium_driver = webdriver.Chrome(options=chrome_options)
            selenium_driver.set_page_load_timeout(30)
            try:
                selenium_driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
                )
            except Exception:
                # Non-fatal if CDP is unavailable in some Selenium/driver combinations.
                pass
            created_driver = True

        try:
            selenium_driver.get("https://theposterdb.com/login")
            # The regular sign-in page can contain challenge-related keywords in embedded scripts.
            # For this initial page load, rely on URL/title checks only to avoid false positives.
            _raise_if_rate_limited(
                selenium_driver.page_source,
                selenium_driver.current_url,
                "login",
                check_content_markers=False,
            )
            WebDriverWait(selenium_driver, 15).until(EC.presence_of_element_located((By.NAME, "login")))

            email_input = selenium_driver.find_element(By.NAME, "login")
            password_input = selenium_driver.find_element(By.NAME, "password")
            email_input.clear()
            email_input.send_keys(Config.TPDB_EMAIL)
            password_input.clear()
            password_input.send_keys(Config.TPDB_PASSWORD)
            password_input.send_keys(Keys.RETURN)

            WebDriverWait(selenium_driver, 20).until(lambda d: not _is_login_url(d.current_url))
            # After sign-in, /feed is expected. Avoid content-marker checks here because
            # regular TPDB pages can include Cloudflare-related script text.
            _raise_if_rate_limited(
                selenium_driver.page_source,
                selenium_driver.current_url,
                "login_submit",
                check_content_markers=False,
            )
            if _is_login_url(selenium_driver.current_url):
                raise RuntimeError("TPDB login failed: still on /login after submitting credentials.")

            if force:
                logging.info("Selenium re-authenticated with ThePosterDB.")
            else:
                logging.info("Selenium initialized and logged into ThePosterDB.")
        except Exception:
            logging.exception("Failed to login to ThePosterDB (force=%s)", force)
            if created_driver:
                teardown_selenium()
            raise

def teardown_selenium():
    """Shutdown Selenium driver (only used on app shutdown)."""
    global selenium_driver
    with selenium_lock:
        if selenium_driver:
            try:
                selenium_driver.quit()
            except Exception:
                pass
            selenium_driver = None

def get_selenium_cookies_as_dict():
    """Return Selenium cookies as a dict for requests.Session."""
    global selenium_driver
    with selenium_lock:
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

        with requests.Session() as session:
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
    for attempt in range(2):
        try:
            with requests.Session() as session:
                session.cookies.update(get_selenium_cookies_as_dict())
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://theposterdb.com/",
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
                })

                logging.debug(f"Converting image to base64: {image_url}")
                response = session.get(image_url, timeout=15)
                if response.status_code == 429 and attempt == 0:
                    logging.warning("TPDB preview image rate limit hit; retrying in %ss.", TPDB_IMAGE_PREVIEW_RETRY_DELAY_SEC)
                    time.sleep(TPDB_IMAGE_PREVIEW_RETRY_DELAY_SEC)
                    continue
                response.raise_for_status()

                image_data = base64.b64encode(response.content).decode('utf-8')
                content_type = response.headers.get('content-type', 'image/jpeg')
                return f"data:{content_type};base64,{image_data}"
        except Exception as e:
            logging.warning(f"Error converting image to base64: {e}")
            return None
    return None


def _normalize_tpdb_text(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def _poster_link_text(poster_link):
    parts = [
        poster_link.get_text(" ", strip=True),
        poster_link.get("title", ""),
        poster_link.get("aria-label", ""),
        poster_link.get("href", ""),
    ]
    current = poster_link.parent
    for _ in range(3):
        if not current:
            break
        parts.extend([
            current.get_text(" ", strip=True),
            current.get("title", ""),
            current.get("aria-label", ""),
        ])
        current = current.parent
    for image in poster_link.find_all("img"):
        parts.extend([
            image.get("alt", ""),
            image.get("title", ""),
            image.get("src", ""),
            image.get("data-src", ""),
        ])
    return _normalize_tpdb_text(" ".join(part for part in parts if part))


def _extract_tpdb_season_key(poster_link):
    text = _poster_link_text(poster_link).lower()
    if re.search(r"\b(specials?|season\s+0|s00)\b", text):
        return "specials"

    patterns = (
        r"\bseason[\s._-]*(\d{1,2})\b",
        r"\bs[\s._-]*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            season_number = int(match.group(1))
            return "specials" if season_number == 0 else str(season_number)

    return None


def _season_key_from_jellyfin(season):
    if season.get("is_special"):
        return "specials"
    season_number = season.get("number", season.get("season_number"))
    if season_number is None:
        return None
    return str(season_number)


def _tpdb_absolute_url(href):
    if not href:
        return None
    if href.startswith('http'):
        return href
    if href.startswith('/'):
        return Config.TPDB_BASE_URL + href
    return None


def _poster_card_container(poster_link):
    current = poster_link
    for _ in range(6):
        if not current:
            return poster_link.parent
        classes = current.get('class') or []
        if 'hovereffect' in classes or current.select_one('div.overlay[data-poster-id]'):
            return current
        current = current.parent
    return poster_link.parent


def _extract_tpdb_card_metadata(poster_link):
    card = _poster_card_container(poster_link)
    set_link = card.select_one('a[href*="/set/"]') if card else None
    uploader_link = card.select_one('.uploaded-by a') if card else None
    overlay = card.select_one('.overlay[data-poster-id]') if card else None
    preview_source = card.select_one('source[srcset], img.tpdb-poster[src]') if card else None
    set_url = _tpdb_absolute_url(set_link.get('href')) if set_link else None
    preview_href = preview_source.get('srcset') or preview_source.get('src') if preview_source else None
    if preview_href:
        preview_href = preview_href.split(',')[0].strip().split(' ')[0]
    preview_url = _tpdb_absolute_url(preview_href)
    set_id = None
    if set_url:
        match = re.search(r"/set/(\d+)", set_url)
        if match:
            set_id = match.group(1)

    return {
        'set_id': set_id,
        'set_url': set_url,
        'set_poster_count': set_link.get_text(strip=True) if set_link else None,
        'uploader': uploader_link.get_text(strip=True) if uploader_link else 'Unknown',
        'tpdb_poster_id': overlay.get('data-poster-id') if overlay else None,
        'tpdb_poster_type': overlay.get('data-poster-type') if overlay else None,
        'preview_url': preview_url,
    }


def _poster_dict(poster_id, poster_url, base64_image=None, target_type="series", season=None, group_id=None, metadata=None):
    metadata = metadata or {}
    poster = {
        'id': poster_id,
        'url': poster_url,
        'base64': base64_image,
        'title': 'Poster',
        'uploader': metadata.get('uploader') or 'Unknown',
        'likes': 0,
        'target_type': target_type,
        'group_id': group_id,
        'set_id': metadata.get('set_id'),
        'set_url': metadata.get('set_url'),
        'set_poster_count': metadata.get('set_poster_count'),
        'tpdb_poster_id': metadata.get('tpdb_poster_id'),
    }
    if season:
        poster.update({
            'season_id': season.get('id'),
            'season_number': season.get('number'),
            'season_title': season.get('title'),
            'is_special': season.get('is_special', False),
            'season_has_poster': season.get('has_poster', False),
        })
    return poster


def _resolve_tpdb_search_query(item_title, item_type=None, tmdb_id=None):
    tmdb_type = None
    if item_type == "Movie":
        tmdb_type = "movie"
    elif item_type == "Series":
        tmdb_type = "tv"

    search_query = item_title
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
    return search_query


def _build_tpdb_search_url(search_query, item_type=None):
    search_url = Config.TPDB_SEARCH_URL_TEMPLATE.format(query=quote_plus(search_query))
    if item_type == "Movie":
        search_url += "&section=movies"
    elif item_type == "Series":
        search_url += "&section=shows"
    return search_url


def _tpdb_url_with_query_params(url, **params):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _get_tpdb_preview_image(image_url, include_base64, preview_url=None):
    if not include_base64:
        return None
    image_source = preview_url or image_url
    if image_source == image_url:
        time.sleep(TPDB_IMAGE_PREVIEW_DELAY_SEC)
    return get_image_as_base64(image_source)


def _open_tpdb_page_with_delay(url):
    time.sleep(TPDB_PAGE_REQUEST_DELAY_SEC)
    selenium_driver.get(url)


def search_tpdb_for_poster_groups(
    item_title,
    item_year=None,
    item_type=None,
    tmdb_id=None,
    eligible_seasons=None,
    max_posters=18,
    max_groups=6,
    include_base64=True,
    requested_set_urls=None,
):
    """Return grouped TPDB poster candidates plus a flat show-poster list."""
    global selenium_driver
    eligible_seasons = eligible_seasons or []
    requested_set_urls = set(requested_set_urls or [])
    season_by_key = {
        _season_key_from_jellyfin(season): season
        for season in eligible_seasons
        if _season_key_from_jellyfin(season)
    }
    search_query = _resolve_tpdb_search_query(item_title, item_type=item_type, tmdb_id=tmdb_id)
    search_url = _build_tpdb_search_url(search_query, item_type=item_type)
    logging.info(f"TPDB search URL: {search_url}")

    try:
        groups = []
        poster_id = 1
        for attempt in range(3):
            try:
                with selenium_lock:
                    if not selenium_driver:
                        setup_selenium_and_login()

                    selenium_driver.get(search_url)
                    current_url = selenium_driver.current_url
                    if _is_login_url(current_url):
                        logging.warning("TPDB session expired on search page for '%s' (%s).", item_title, current_url)
                        raise TPDBSessionExpired("TPDB session expired while loading search page.")

                    _raise_if_rate_limited(selenium_driver.page_source, current_url, "search_page")
                    _wait_for_search_results_ready(selenium_driver, timeout=15)
                    soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')

                    search_result_links = soup.select(SEARCH_RESULT_SELECTOR)
                    if not search_result_links:
                        logging.info(f"No TPDB search results for '{search_query}'.")
                        return {'posters': [], 'groups': [], 'best_group': None, 'search_query': search_query}

                    expected_year = extract_title_year(search_query) or (str(item_year) if item_year else None)
                    expected_title = strip_title_year(search_query)
                    expected_title_norm = normalize_title_for_comparison(expected_title)
                    candidate_links = []
                    year_mismatch_count = 0
                    for index, link in enumerate(search_result_links):
                        try:
                            title_element = link.find(class_="text-truncate") or link.find("span") or link
                            result_title = title_element.get_text(strip=True) if title_element else link.get_text(strip=True)
                            display_title = format_title_year_spacing(result_title)
                            result_year = extract_title_year(result_title)
                            result_title_norm = normalize_title_for_comparison(strip_title_year(result_title))
                            exact_title_match = bool(expected_title_norm and expected_title_norm == result_title_norm)
                            if expected_year and result_year and result_year != expected_year:
                                year_mismatch_count += 1
                                logging.debug("Skipping TPDB result for '%s' due to year mismatch: %s", search_query, display_title)
                                continue
                            item_page_path = link.get('href')
                            target_item_page_url = item_page_path if item_page_path and item_page_path.startswith('http') else (
                                Config.TPDB_BASE_URL + item_page_path if item_page_path and item_page_path.startswith('/') else None
                            )
                            if not target_item_page_url:
                                continue
                            candidate_links.append({
                                'title': display_title,
                                'year': result_year,
                                'score': calculate_title_match_score(search_query, result_title),
                                'exact_title_match': exact_title_match,
                                'exact_year_match': exact_title_match and (not expected_year or result_year == expected_year),
                                'url': target_item_page_url,
                                'index': index,
                            })
                        except Exception:
                            continue

                    if year_mismatch_count:
                        logging.debug("Skipped %d TPDB result(s) for '%s' due to year mismatch.", year_mismatch_count, search_query)

                    exact_matches = sorted(
                        [candidate for candidate in candidate_links if candidate['exact_year_match']],
                        key=lambda candidate: candidate['index'],
                    )
                    strong_matches = [candidate for candidate in candidate_links if candidate['score'] >= 0.8]
                    fallback_matches = sorted(
                        strong_matches or candidate_links,
                        key=lambda candidate: (-candidate['score'], candidate['index'])
                    )
                    if exact_matches:
                        logging.info(
                            "Found %d exact TPDB result(s) for '%s'; checking exact matches first.",
                            len(exact_matches),
                            search_query,
                        )

                    queued_candidate_indexes = set()
                    candidates_to_check = []
                    for candidate in exact_matches + fallback_matches:
                        if candidate['index'] in queued_candidate_indexes:
                            continue
                        candidates_to_check.append(candidate)
                        queued_candidate_indexes.add(candidate['index'])
                        if len(candidates_to_check) >= max_groups:
                            break

                    for candidate in candidates_to_check:
                        logging.info(
                            "Checking TPDB result for '%s': %s (%d%% match)",
                            search_query,
                            candidate['title'],
                            round(candidate['score'] * 100),
                        )
                        _open_tpdb_page_with_delay(candidate['url'])
                        current_url = selenium_driver.current_url
                        if _is_login_url(current_url):
                            logging.warning("TPDB session expired on item page for '%s' (%s).", item_title, current_url)
                            raise TPDBSessionExpired("TPDB session expired while loading item page.")

                        _raise_if_rate_limited(selenium_driver.page_source, current_url, "item_page")
                        try:
                            _wait_for_item_posters_ready(selenium_driver, timeout=15)
                        except TimeoutError:
                            logging.warning("Timed out checking TPDB result '%s' for '%s'; trying next result.", candidate['title'], search_query)
                            continue

                        item_soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')
                        group = {
                            'id': f"group-{candidate['index']}",
                            'title': candidate['title'],
                            'url': candidate['url'],
                            'match_score': candidate['score'],
                            'source_index': candidate['index'],
                            'show_posters': [],
                            'season_posters': [],
                            'eligible_season_count': len(season_by_key),
                            'covered_season_count': 0,
                            'covered_season_keys': [],
                            'available_sets': [],
                        }
                        discovered_set_urls = []
                        discovered_set_lookup = {}
                        discovered_set_order = {}

                        for poster_link in item_soup.select(ITEM_POSTER_SELECTOR)[:Config.MAX_POSTERS_PER_ITEM]:
                            href = poster_link.get('href')
                            poster_url = _tpdb_absolute_url(href)
                            if not poster_url:
                                continue

                            metadata = _extract_tpdb_card_metadata(poster_link)
                            set_url = metadata.get('set_url')
                            if set_url and set_url not in discovered_set_lookup:
                                discovered_set_order[set_url] = len(discovered_set_urls)
                                discovered_set_urls.append(set_url)
                                discovered_set_lookup[set_url] = {
                                    'set_id': metadata.get('set_id'),
                                    'set_url': set_url,
                                    'set_poster_count': metadata.get('set_poster_count'),
                                    'uploader': metadata.get('uploader') or 'Unknown',
                                }
                            if requested_set_urls and set_url not in requested_set_urls:
                                continue
                            if not requested_set_urls and set_url and discovered_set_order.get(set_url, 0) >= max_posters:
                                continue
                            base64_image = _get_tpdb_preview_image(poster_url, include_base64, metadata.get('preview_url'))
                            season_key = _extract_tpdb_season_key(poster_link)
                            if season_key and season_key in season_by_key:
                                season = season_by_key[season_key]
                                group['season_posters'].append(_poster_dict(
                                    poster_id,
                                    poster_url,
                                    base64_image=base64_image,
                                    target_type='season',
                                    season=season,
                                    group_id=group['id'],
                                    metadata=metadata,
                                ))
                                poster_id += 1
                            elif not season_key:
                                group['show_posters'].append(_poster_dict(
                                    poster_id,
                                    poster_url,
                                    base64_image=base64_image,
                                    target_type='series',
                                    group_id=group['id'],
                                    metadata=metadata,
                                ))
                                poster_id += 1

                        group['available_sets'] = list(discovered_set_lookup.values())
                        if discovered_set_urls and season_by_key:
                            set_urls_to_load = [
                                set_url for set_url in discovered_set_urls
                                if not requested_set_urls or set_url in requested_set_urls
                            ][:max_posters]
                            seen_poster_urls = {
                                poster.get('url')
                                for poster in group['show_posters'] + group['season_posters']
                            }
                            for set_url in set_urls_to_load:
                                logging.info("Checking TPDB poster set for '%s': %s", search_query, set_url)
                                _open_tpdb_page_with_delay(set_url)
                                current_url = selenium_driver.current_url
                                if _is_login_url(current_url):
                                    logging.warning("TPDB session expired on set page for '%s' (%s).", item_title, current_url)
                                    raise TPDBSessionExpired("TPDB session expired while loading set page.")

                                _raise_if_rate_limited(selenium_driver.page_source, current_url, "set_page")
                                try:
                                    _wait_for_item_posters_ready(selenium_driver, timeout=10)
                                except TimeoutError:
                                    continue

                                set_soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')
                                for poster_link in set_soup.select(ITEM_POSTER_SELECTOR)[:max(max_posters * max(len(season_by_key) + 1, 1), max_posters)]:
                                    poster_url = _tpdb_absolute_url(poster_link.get('href'))
                                    if not poster_url or poster_url in seen_poster_urls:
                                        continue

                                    metadata = _extract_tpdb_card_metadata(poster_link)
                                    season_key = _extract_tpdb_season_key(poster_link)
                                    poster_type = (metadata.get('tpdb_poster_type') or '').lower()
                                    if season_key and season_key in season_by_key:
                                        season = season_by_key[season_key]
                                        base64_image = _get_tpdb_preview_image(poster_url, include_base64, metadata.get('preview_url'))
                                        group['season_posters'].append(_poster_dict(
                                            poster_id,
                                            poster_url,
                                            base64_image=base64_image,
                                            target_type='season',
                                            season=season,
                                            group_id=group['id'],
                                            metadata=metadata,
                                        ))
                                        poster_id += 1
                                        seen_poster_urls.add(poster_url)
                                    elif poster_type == 'show':
                                        base64_image = _get_tpdb_preview_image(poster_url, include_base64, metadata.get('preview_url'))
                                        group['show_posters'].append(_poster_dict(
                                            poster_id,
                                            poster_url,
                                            base64_image=base64_image,
                                            target_type='series',
                                            group_id=group['id'],
                                            metadata=metadata,
                                        ))
                                        poster_id += 1
                                        seen_poster_urls.add(poster_url)

                        should_fallback_to_season_pages = season_by_key and (
                            not discovered_set_urls or not group['season_posters']
                        )
                        if should_fallback_to_season_pages:
                            logging.info(
                                "No usable TPDB set season posters found for '%s'; falling back to season-filtered pages.",
                                search_query,
                            )

                        for season_key, season in (season_by_key.items() if should_fallback_to_season_pages else []):
                            season_param = 0 if season_key == "specials" else season_key
                            season_url = _tpdb_url_with_query_params(
                                candidate['url'],
                                textless="All",
                                language="all",
                                season=season_param,
                                sort="Downloads",
                                variation="orig",
                            )
                            logging.info(
                                "Checking TPDB season posters for '%s': %s season %s",
                                search_query,
                                candidate['title'],
                                season_param,
                            )
                            _open_tpdb_page_with_delay(season_url)
                            current_url = selenium_driver.current_url
                            if _is_login_url(current_url):
                                logging.warning("TPDB session expired on season page for '%s' (%s).", item_title, current_url)
                                raise TPDBSessionExpired("TPDB session expired while loading season page.")

                            _raise_if_rate_limited(selenium_driver.page_source, current_url, "season_page")
                            try:
                                _wait_for_item_posters_ready(selenium_driver, timeout=10)
                            except TimeoutError:
                                continue

                            season_soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')
                            seen_season_urls = {
                                poster.get('url')
                                for poster in group['season_posters']
                                if _season_key_from_jellyfin(poster) == season_key
                            }
                            for poster_link in season_soup.select(ITEM_POSTER_SELECTOR)[:max_posters]:
                                href = poster_link.get('href')
                                poster_url = _tpdb_absolute_url(href)
                                if not poster_url or poster_url in seen_season_urls:
                                    continue

                                metadata = _extract_tpdb_card_metadata(poster_link)
                                base64_image = _get_tpdb_preview_image(poster_url, include_base64, metadata.get('preview_url'))
                                group['season_posters'].append(_poster_dict(
                                    poster_id,
                                    poster_url,
                                    base64_image=base64_image,
                                    target_type='season',
                                    season=season,
                                    group_id=group['id'],
                                    metadata=metadata,
                                ))
                                seen_season_urls.add(poster_url)
                                poster_id += 1

                        covered_keys = {
                            _season_key_from_jellyfin(poster)
                            for poster in group['season_posters']
                            if _season_key_from_jellyfin(poster)
                        }
                        group['covered_season_keys'] = sorted(covered_keys)
                        group['covered_season_count'] = len(covered_keys)
                        if (group['show_posters'] or group['season_posters']) and group not in groups:
                            groups.append(group)
                            break

                    break
            except TPDBSessionExpired:
                if attempt == 0:
                    logging.warning("TPDB session expired; re-authenticating and retrying once for '%s'.", item_title)
                    setup_selenium_and_login(force=True)
                    continue
                raise
            except TPDBRateLimited:
                if attempt < 2:
                    backoff_sec = 2 + attempt * 2
                    logging.warning("TPDB challenge/rate-limit detected for '%s'; retrying in %ss.", item_title, backoff_sec)
                    time.sleep(backoff_sec)
                    continue
                raise

        groups = sorted(groups, key=lambda group: (-group['covered_season_count'], -group['match_score'], group['source_index']))
        best_group = groups[0] if groups else None
        posters = []
        if best_group:
            posters = best_group['show_posters'][:max_posters]
        logging.info(f"Found {len(posters)} poster links.")
        return {
            'posters': posters,
            'groups': groups,
            'best_group': best_group,
            'search_query': search_query,
        }
    except Exception:
        logging.exception("Error during TPDB scraping: search_url=%s current_url=%s", search_url, _get_selenium_current_url())
        raise


def search_tpdb_for_posters_multiple(item_title, item_year=None, item_type=None, tmdb_id=None, max_posters=18):
    """
    Return up to max_posters show/movie poster URLs with base64 data for preview.
    item_type should be "Movie" or "Series" (Jellyfin item Type).
    """
    result = search_tpdb_for_poster_groups(
        item_title,
        item_year=item_year,
        item_type=item_type,
        tmdb_id=tmdb_id,
        eligible_seasons=[],
        max_posters=max_posters,
        max_groups=6,
        include_base64=True,
    )
    return result.get('posters', [])

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

def extract_title_year(title):
    if not title:
        return None
    year_match = re.search(r'\((\d{4})\)', title)
    return year_match.group(1) if year_match else None

def strip_title_year(title):
    if not title:
        return ""
    return re.sub(r'\s*\(\d{4}\)\s*', ' ', title).strip()

def format_title_year_spacing(title):
    if not title:
        return ""
    return re.sub(r'\s*\((\d{4})\)', r' (\1)', title).strip()

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


def _parse_jellyfin_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def get_jellyfin_seasons(series_id):
    """Return eligible Jellyfin seasons for a Series item, including Specials unless future-dated."""
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY or not series_id:
        return []

    headers = {
        "X-Emby-Token": Config.JELLYFIN_API_KEY,
        "Accept": "application/json",
    }
    seasons_url = (
        f"{Config.JELLYFIN_URL}/Shows/{series_id}/Seasons"
        "?Fields=Id,Name,IndexNumber,PremiereDate,ImageTags"
    )
    try:
        response = requests.get(seasons_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        seasons = []
        now = datetime.utcnow()
        for season in data.get('Items', []):
            premiere_date = _parse_jellyfin_datetime(season.get('PremiereDate'))
            if premiere_date and premiere_date > now:
                continue

            season_id = season.get('Id')
            if not season_id:
                continue

            season_number = season.get('IndexNumber')
            has_primary = bool(season.get('ImageTags', {}).get('Primary'))
            thumbnail_url = None
            if has_primary:
                thumbnail_url = (
                    f"{Config.JELLYFIN_URL}/Items/{season_id}/Images/Primary"
                    f"?maxWidth=300&quality=85&tag={season['ImageTags']['Primary']}"
                )

            seasons.append({
                'id': season_id,
                'title': season.get('Name') or ('Specials' if season_number == 0 else f"Season {season_number}"),
                'number': season_number,
                'is_special': season_number == 0,
                'premiere_date': season.get('PremiereDate'),
                'has_poster': has_primary,
                'thumbnail_url': thumbnail_url,
            })
        return seasons
    except Exception as e:
        logging.warning(f"Could not fetch seasons for Jellyfin series {series_id}: {e}")
        return []


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
                f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,Type,ChildCount"
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
                        "season_count": item.get('ChildCount') if item.get('Type') == 'Series' else None,
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
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,ChildCount"
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
                        "season_count": item.get('ChildCount'),
                        'ProviderIds': item.get('ProviderIds', {})
                    })
    except Exception as e:
        logging.error(f"Error fetching items from Jellyfin: {e}")
        return []

    logging.info(f"Total items fetched: {len(items)}")
    return items
