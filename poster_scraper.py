import requests
import json
from io import BytesIO
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import time
import re
import os
import hashlib
import base64
from datetime import datetime
import threading
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
try:
    from config import Config
except ModuleNotFoundError:
    from config_example import Config
from safe_http import ImageLoginRequired, fetch_image, origin
import logging
from requests.exceptions import ChunkedEncodingError, ConnectionError

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

if Config.JELLYFIN_URL:
    Config.JELLYFIN_URL = Config.JELLYFIN_URL.rstrip('/')


def get_jellyfin_headers(**headers):
    """Build headers for Jellyfin API-key authentication (including Jellyfin 12+)."""
    return {
        'Authorization': f'MediaBrowser Token="{Config.JELLYFIN_API_KEY}"',
        **headers,
    }


# Global Selenium driver
selenium_driver = None
selenium_lock = threading.RLock()

SEARCH_RESULT_SELECTOR = "a.btn.btn-dark-lighter.flex-grow-1.text-truncate.py-2.text-left.position-relative"
ITEM_POSTER_SELECTOR = "a.bg-transparent.border-0.text-white"
TPDB_PAGE_REQUEST_DELAY_SEC = 1.25
TPDB_ITEM_MAX_PAGES = 3  # Poster pages list ~24 cards each; later pages are only read up to this limit.
TPDB_IMAGE_PREVIEW_DELAY_SEC = 0.75
TPDB_IMAGE_PREVIEW_RETRY_DELAY_SEC = 3
TPDB_PREVIEW_MAX_SIZE = (160, 242)
TPDB_PREVIEW_QUALITY = 85
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
    """Raised when TPDb redirects Selenium back to /login."""


class TPDBRateLimited(Exception):
    """Raised when TPDb responds with a likely rate-limit/challenge page."""


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
        logging.warning(f"Failed to write TPDb debug snapshot: {snapshot_error}")
        return None


def _raise_if_rate_limited(page_source, current_url, context_label="search", check_content_markers=True):
    page_lower = (page_source or "").lower()
    title = (_extract_html_title(page_source) or "unknown title")
    title_lower = title.lower()
    title_looks_challenge = any(marker in title_lower for marker in RATE_LIMIT_TITLE_MARKERS)
    page_looks_challenge = check_content_markers and any(marker in page_lower for marker in RATE_LIMIT_MARKERS)

    if _is_rate_limit_url(current_url) or title_looks_challenge or page_looks_challenge:
        snapshot_path = _write_tpdb_debug_snapshot(page_source, f"{context_label}_challenge")
        details = f"TPDb returned a rate-limit/challenge page at {current_url or 'unknown URL'} (title: {title})"
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
        raise TimeoutError("Timed out waiting for TPDb search results page to load.") from exc


def _wait_for_item_posters_ready(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, ITEM_POSTER_SELECTOR)
            or "no posters" in (d.page_source or "").lower()
            or "no poster" in (d.page_source or "").lower()
        )
    except TimeoutException as exc:
        _raise_if_rate_limited(driver.page_source, driver.current_url, "item_timeout")
        raise TimeoutError("Timed out waiting for TPDb item poster page to load.") from exc


def _get_selenium_current_url():
    global selenium_driver
    if not selenium_driver:
        return None
    try:
        return selenium_driver.current_url
    except Exception:
        return None


class BrowserUnavailable(RuntimeError):
    """Chrome/Chromium or its driver could not be started."""


def _configured_path(*names):
    for name in names:
        value = os.environ.get(name) or getattr(Config, name, None)
        if value:
            return name, os.path.expanduser(str(value).strip())
    return None, None


def _chrome_binary():
    name, path = _configured_path("CHROME_BINARY")
    if path and not os.path.isfile(path):
        raise BrowserUnavailable(f"{name}={path} does not exist. Point it at your Chrome/Chromium executable or unset it.")
    return path


def _chrome_service():
    """Build a ChromeDriver service, honouring CHROMEDRIVER_PATH / SE_CHROMEDRIVER.

    Without either, Selenium Manager locates (or downloads) a matching driver itself.
    """
    log_output = None if getattr(Config, "DEBUG", False) else os.devnull
    name, path = _configured_path("CHROMEDRIVER_PATH", "SE_CHROMEDRIVER")
    if path:
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise BrowserUnavailable(f"{name}={path} is not an executable chromedriver.")
        return Service(executable_path=path, log_output=log_output)
    return Service(log_output=log_output)


def _start_chrome(chrome_options, chrome_binary):
    service = _chrome_service()
    try:
        return webdriver.Chrome(options=chrome_options, service=service)
    except (WebDriverException, OSError) as error:
        detail = (getattr(error, "msg", None) or str(error) or type(error).__name__).strip().splitlines()[0]
        raise BrowserUnavailable(
            "Could not start headless Chrome for ThePosterDB "
            f"(browser: {chrome_binary or 'auto-detect'}, driver: {getattr(service, 'path', None) or 'auto-detect'}): {detail}. "
            "Install Chromium and a matching chromedriver, then set CHROME_BINARY / CHROMEDRIVER_PATH if they are not on PATH."
        ) from error


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
            chrome_binary = _chrome_binary()
            if chrome_binary:
                chrome_options.binary_location = chrome_binary
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-gpu")
            if not getattr(Config, "DEBUG", False):
                chrome_options.add_argument("--disable-logging")
                chrome_options.add_argument("--log-level=3")
                chrome_options.add_argument("--disable-webgpu")
                chrome_options.add_argument("--disable-vulkan")
                chrome_options.add_argument("--disable-features=WebGPU,Vulkan,UseSkiaRenderer")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("--lang=en-US")
            chrome_options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            excluded_switches = ["enable-automation"]
            if not getattr(Config, "DEBUG", False):
                excluded_switches.append("enable-logging")
            chrome_options.add_experimental_option("excludeSwitches", excluded_switches)
            chrome_options.add_experimental_option("useAutomationExtension", False)
            selenium_driver = _start_chrome(chrome_options, chrome_binary)
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
            # regular TPDb pages can include Cloudflare-related script text.
            _raise_if_rate_limited(
                selenium_driver.page_source,
                selenium_driver.current_url,
                "login_submit",
                check_content_markers=False,
            )
            if _is_login_url(selenium_driver.current_url):
                raise RuntimeError("TPDb login failed: still on /login after submitting credentials.")

            if force:
                logging.info("Selenium re-authenticated with ThePosterDB.")
            else:
                logging.info("Selenium initialized and logged into ThePosterDB.")
        except Exception:
            logging.exception("Failed to login to ThePosterDB (force=%s)", force)
            if created_driver:
                teardown_selenium()
            raise

def teardown_selenium(timeout=5):
    """Shutdown Selenium driver without letting a stuck ChromeDriver block app exit."""
    global selenium_driver
    with selenium_lock:
        driver = selenium_driver
        selenium_driver = None

    if not driver:
        return

    def quit_driver():
        try:
            driver.quit()
        except Exception as shutdown_error:
            logging.warning(f"Failed to shutdown Selenium driver cleanly: {shutdown_error}")

    cleanup_thread = threading.Thread(target=quit_driver, daemon=True)
    cleanup_thread.start()
    cleanup_thread.join(timeout)
    if cleanup_thread.is_alive():
        logging.warning("Selenium driver shutdown is still running; continuing application exit.")

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

def get_tpdb_cookie_jar():
    """Keep Selenium's domain/path restrictions; never make hostless cookies."""
    jar = requests.cookies.RequestsCookieJar()
    with selenium_lock:
        cookies = selenium_driver.get_cookies() if selenium_driver else []
    for cookie in cookies:
        domain = cookie.get('domain', '')
        if domain.lstrip('.') != 'theposterdb.com':
            continue
        jar.set(cookie['name'], cookie['value'], domain=domain,
                path=cookie.get('path', '/'), secure=cookie.get('secure', True))
    return jar


def fetch_tpdb_image(url):
    return fetch_image(url, 'tpdb', cookies=get_tpdb_cookie_jar(), headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': Config.TPDB_BASE_URL + '/',
        # Prefer lossless/JPEG originals so uploads don't go through a lossy WebP->JPEG round trip.
        'Accept': 'image/jpeg,image/png;q=0.9,image/webp;q=0.5,image/avif;q=0.4',
    })


def download_image_with_cookies(url, save_path):
    try:
        image_bytes, _ = fetch_tpdb_image(url)
        os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)
        # Uploads are JPEG for compatibility with every Jellyfin client. Upright JPEGs are
        # stored byte-for-byte; other formats (WebP, AVIF, PNG, ...) are converted once.
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            orientation = image.getexif().get(0x0112, 1)
            if image.format == 'JPEG' and image.mode in ('RGB', 'L') and orientation == 1:
                with open(save_path, 'wb') as handle:
                    handle.write(image_bytes)
                return True
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.save(save_path, format='JPEG', quality=95)
        return True
    except ImageLoginRequired:
        raise
    except Exception as e:
        logging.error('Error downloading poster: %s', e)
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
        response = requests.get(url, headers=get_jellyfin_headers(), timeout=10)
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

def _compress_image_preview(image_bytes, max_size=TPDB_PREVIEW_MAX_SIZE, quality=TPDB_PREVIEW_QUALITY):
    if not Image or not ImageOps:
        return None
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image = ImageOps.fit(image, max_size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            if image.mode not in ('RGB', 'RGBA'):
                image = image.convert('RGB')

            output = BytesIO()
            try:
                image.save(output, format='WEBP', quality=quality, method=4)
                return 'image/webp', output.getvalue()
            except Exception:
                output = BytesIO()
                if image.mode == 'RGBA':
                    image = image.convert('RGB')
                image.save(output, format='JPEG', quality=quality, optimize=True)
                return 'image/jpeg', output.getvalue()
    except Exception as e:
        logging.debug(f"Failed to compress preview image: {e}")
        return None


def get_image_as_base64(image_url, max_size=TPDB_PREVIEW_MAX_SIZE, quality=TPDB_PREVIEW_QUALITY):
    """
    Download image and convert to base64 data URL for embedding in UI.
    """
    for attempt in range(2):
        try:
            image_bytes, content_type = fetch_tpdb_image(image_url)
            if max_size:
                compressed = _compress_image_preview(image_bytes, max_size=max_size, quality=quality)
                if compressed:
                    content_type, image_bytes = compressed
            image_data = base64.b64encode(image_bytes).decode('utf-8')
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
    url = Config.TPDB_BASE_URL + href if href.startswith('/') else href
    try:
        if origin(url) in {origin(Config.TPDB_BASE_URL), ('https', 'images.theposterdb.com', 443)}:
            return url
    except ValueError:
        pass
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
    title_element = card.select_one('.poster-title-correction p') if card else None
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
        'uploader': uploader_link.get_text(strip=True) if uploader_link else 'Unknown',
        'tpdb_poster_id': overlay.get('data-poster-id') if overlay else None,
        'tpdb_poster_type': overlay.get('data-poster-type') if overlay else None,
        'tpdb_title': format_title_year_spacing(title_element.get_text(strip=True)) if title_element else None,
        'preview_url': preview_url,
    }


def _order_by_preferred_uploaders(poster_links, preferred_uploaders):
    """Stable-sort TPDb cards so earlier entries in ``preferred_uploaders`` come first."""
    poster_links = list(poster_links)
    if not preferred_uploaders:
        return poster_links
    ranks = {}
    for index, name in enumerate(preferred_uploaders):
        ranks.setdefault(str(name).casefold(), index)
    fallback = len(ranks)

    def sort_key(entry):
        index, link = entry
        uploader = _extract_tpdb_card_metadata(link).get('uploader') or ''
        return ranks.get(uploader.casefold(), fallback), index

    return [link for _, link in sorted(enumerate(poster_links), key=sort_key)]


def _tpdb_card_matches_item(metadata, expected_title_norm, expected_year=None):
    card_title = metadata.get('tpdb_title')
    if not card_title:
        return True

    card_year = extract_title_year(card_title)
    if expected_year and card_year and card_year != expected_year:
        return False

    card_title_norm = normalize_title_for_comparison(strip_title_year(card_title))
    if not expected_title_norm or not card_title_norm:
        return True

    return (
        card_title_norm == expected_title_norm or
        card_title_norm.startswith(f"{expected_title_norm} season") or
        card_title_norm.startswith(f"{expected_title_norm} specials")
    )


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
        'tpdb_poster_id': metadata.get('tpdb_poster_id'),
        'source_url': metadata.get('source_url'),
        'preview_url': metadata.get('preview_url'),
        'preview_needs_load': base64_image is None,
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
    tmdb_type = {"Movie": "movie", "Series": "tv", "Collection": "collection"}.get(item_type)

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
            elif tmdb_type == "collection":
                # Collections span years; TPDb lists them without one.
                tmdb_title = tmdb_data.get("name")
                year = ""
            else:
                tmdb_title = tmdb_data.get("title")
                year = (tmdb_data.get("release_date") or "")[:4]
            if tmdb_title:
                search_query = f'{tmdb_title} ({year})' if year else tmdb_title
                logging.debug(f"Using TMDB title for TPDb search: {search_query}")
        except Exception as e:
            logging.warning(f"TMDB lookup failed for {item_title} ({item_type}): {e}; falling back to Jellyfin title.")
    return search_query


def _build_tpdb_search_url(search_query, item_type=None):
    search_url = Config.TPDB_SEARCH_URL_TEMPLATE.format(query=quote_plus(search_query))
    if item_type == "Movie":
        search_url += "&section=movies"
    elif item_type == "Series":
        search_url += "&section=shows"
    elif item_type == "Collection":
        search_url += "&section=collections"
    return search_url


def _tpdb_search_term(search_query):
    return strip_title_year(search_query) or search_query


def _tpdb_url_with_query_params(url, **params):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _get_tpdb_preview_image(image_url, include_base64, preview_url=None, cache=None, max_size=TPDB_PREVIEW_MAX_SIZE, quality=TPDB_PREVIEW_QUALITY):
    if not include_base64:
        return None
    image_source = preview_url or image_url
    cache_key = (image_source, max_size, quality)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    if image_source == image_url:
        time.sleep(TPDB_IMAGE_PREVIEW_DELAY_SEC)
    base64_image = get_image_as_base64(image_source, max_size=max_size, quality=quality)
    if cache is not None:
        cache[cache_key] = base64_image
    return base64_image


def _open_tpdb_page_with_delay(url):
    time.sleep(TPDB_PAGE_REQUEST_DELAY_SEC)
    selenium_driver.get(url)


def _tpdb_next_page_url(soup):
    """URL behind the poster list's "next page" link, or None on the last page."""
    link = soup.select_one('a[rel="next"]')
    return _tpdb_absolute_url(link.get('href')) if link else None


def _load_more_tpdb_item_cards(item_soup):
    """Follow "next page" links from an open TPDb poster page, up to TPDB_ITEM_MAX_PAGES pages in all."""
    cards = item_soup.select(ITEM_POSTER_SELECTOR)
    next_url = _tpdb_next_page_url(item_soup)
    for _ in range(TPDB_ITEM_MAX_PAGES - 1):
        if not next_url:
            break
        _open_tpdb_page_with_delay(next_url)
        current_url = selenium_driver.current_url
        if _is_login_url(current_url):
            raise TPDBSessionExpired("TPDb session expired while loading item page.")
        _raise_if_rate_limited(selenium_driver.page_source, current_url, "item_page")
        try:
            _wait_for_item_posters_ready(selenium_driver, timeout=10)
        except TimeoutError:
            logging.warning("Timed out loading TPDb page %s; using the pages already loaded.", next_url)
            break
        page_soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')
        cards += page_soup.select(ITEM_POSTER_SELECTOR)
        next_url = _tpdb_next_page_url(page_soup)
    return cards


def search_tpdb_for_poster_groups(item_title, *args, **kwargs):
    """Search TPDb; if a TMDB-derived title finds nothing usable, retry once with the Jellyfin title."""
    result = _search_tpdb_for_poster_groups(item_title, *args, **kwargs)
    if (result.get('best_group') or kwargs.get('tpdb_item_url') or not kwargs.get('tmdb_id')
            or not result.get('search_query') or result['search_query'] == item_title):
        return result
    logging.info("No usable TPDb match for TMDB title '%s'; retrying with Jellyfin title '%s'.",
                 result['search_query'], item_title)
    fallback = _search_tpdb_for_poster_groups(item_title, *args, **dict(kwargs, tmdb_id=None))
    return fallback if fallback.get('best_group') else result


def _search_tpdb_for_poster_groups(
    item_title,
    item_year=None,
    item_type=None,
    tmdb_id=None,
    eligible_seasons=None,
    max_posters=18,
    max_groups=6,
    include_base64=True,
    requested_set_urls=None,
    tpdb_item_url=None,
    cached_available_sets=None,
    preview_max_size=TPDB_PREVIEW_MAX_SIZE,
    preview_quality=TPDB_PREVIEW_QUALITY,
    require_exact=False,
    preferred_uploaders=None,
):
    """Return grouped TPDb poster candidates plus a flat show-poster list.

    ``preferred_uploaders`` (ordered, case-insensitive) moves those uploaders' cards and sets
    to the front, so they are the ones kept when ``max_posters`` limits the sets considered.
    """
    global selenium_driver
    eligible_seasons = eligible_seasons or []
    requested_set_urls = set(requested_set_urls or [])
    cached_sets_by_id = {
        str(set_info.get('set_id') or ''): set_info
        for set_info in (cached_available_sets or [])
        if set_info.get('set_id')
    }
    season_by_key = {
        _season_key_from_jellyfin(season): season
        for season in eligible_seasons
        if _season_key_from_jellyfin(season)
    }
    search_query = _resolve_tpdb_search_query(item_title, item_type=item_type, tmdb_id=tmdb_id)
    search_term = _tpdb_search_term(search_query)
    search_url = _build_tpdb_search_url(search_term, item_type=item_type)
    tpdb_item_url = _tpdb_absolute_url(tpdb_item_url) if tpdb_item_url else None
    if tpdb_item_url:
        logging.info(f"Using saved TPDb page for '{search_query}': {tpdb_item_url}")
    else:
        logging.info(f"TPDb search URL: {search_url}")

    try:
        groups = []
        poster_id = 1
        preview_image_cache = {}
        set_discovery_limit = max(
            max_posters,
            int(getattr(Config, "MAX_TPDB_SETS_PER_ITEM", Config.MAX_POSTERS_PER_ITEM))
        )
        for attempt in range(3):
            try:
                with selenium_lock:
                    if not selenium_driver:
                        setup_selenium_and_login()

                    is_collection = item_type == "Collection"
                    expected_year = None if is_collection else (
                        extract_title_year(search_query) or (str(item_year) if item_year else None))
                    expected_title = strip_title_year(search_query)
                    expected_title_norm = _comparison_title(expected_title, is_collection)
                    if tpdb_item_url:
                        candidates_to_check = [{
                            'title': search_query,
                            'year': expected_year,
                            'score': 1.0,
                            'exact_title_match': True,
                            'exact_year_match': True,
                            'url': tpdb_item_url,
                            'index': 0,
                        }]
                    else:
                        selenium_driver.get(search_url)
                        current_url = selenium_driver.current_url
                        if _is_login_url(current_url):
                            logging.warning("TPDb session expired on search page for '%s' (%s).", item_title, current_url)
                            raise TPDBSessionExpired("TPDb session expired while loading search page.")

                        _raise_if_rate_limited(selenium_driver.page_source, current_url, "search_page")
                        _wait_for_search_results_ready(selenium_driver, timeout=15)
                        soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')

                        search_result_links = soup.select(SEARCH_RESULT_SELECTOR)
                        if not search_result_links:
                            logging.info(f"No TPDb search results for '{search_query}'.")
                            return {'posters': [], 'groups': [], 'best_group': None, 'search_query': search_query}

                        candidate_links = []
                        year_mismatch_count = 0
                        for index, link in enumerate(search_result_links):
                            try:
                                title_element = link.find(class_="text-truncate") or link.find("span") or link
                                result_title = title_element.get_text(strip=True) if title_element else link.get_text(strip=True)
                                display_title = format_title_year_spacing(result_title)
                                result_year = extract_title_year(result_title)
                                result_title_norm = _comparison_title(strip_title_year(result_title), is_collection)
                                exact_title_match = bool(expected_title_norm and expected_title_norm == result_title_norm)
                                if expected_year and result_year and result_year != expected_year:
                                    year_mismatch_count += 1
                                    logging.debug("Skipping TPDb result for '%s' due to year mismatch: %s", search_query, display_title)
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
                                    'score': calculate_title_match_score(expected_title, result_title),
                                    'exact_title_match': exact_title_match,
                                    'exact_year_match': exact_title_match and (not expected_year or result_year == expected_year),
                                    'url': target_item_page_url,
                                    'index': index,
                                })
                            except Exception:
                                continue

                        if year_mismatch_count:
                            logging.debug("Skipped %d TPDb result(s) for '%s' due to year mismatch.", year_mismatch_count, search_query)

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
                                "Found %d exact TPDb result(s) for '%s'; checking exact matches first.",
                                len(exact_matches),
                                search_query,
                            )

                        if require_exact:
                            if (not expected_year and not is_collection) or len(exact_matches) != 1:
                                return {'posters': [], 'groups': [], 'best_group': None,
                                        'review_needed': True, 'search_query': search_query}
                            fallback_matches = []
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
                            "Checking TPDb result for '%s': %s (%d%% match)",
                            search_query,
                            candidate['title'],
                            round(candidate['score'] * 100),
                        )
                        _open_tpdb_page_with_delay(candidate['url'])
                        current_url = selenium_driver.current_url
                        if _is_login_url(current_url):
                            logging.warning("TPDb session expired on item page for '%s' (%s).", item_title, current_url)
                            raise TPDBSessionExpired("TPDb session expired while loading item page.")

                        _raise_if_rate_limited(selenium_driver.page_source, current_url, "item_page")
                        try:
                            _wait_for_item_posters_ready(selenium_driver, timeout=15)
                        except TimeoutError:
                            logging.warning("Timed out checking TPDb result '%s' for '%s'; trying next result.", candidate['title'], search_query)
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
                            # Season posters for seasons Jellyfin doesn't have (yet), kept as placeholders.
                            'unmatched_season_posters': [],
                        }

                        def remember_unmatched_season(season_key, url, metadata):
                            if any(entry['season_key'] == season_key and entry.get('set_id') == metadata.get('set_id')
                                   for entry in group['unmatched_season_posters']):
                                return
                            group['unmatched_season_posters'].append({
                                'season_key': season_key, 'url': url, 'set_id': metadata.get('set_id'),
                                'set_url': metadata.get('set_url'), 'uploader': metadata.get('uploader'),
                            })
                        discovered_set_urls = []
                        discovered_set_lookup = {}
                        discovered_set_order = {}

                        # Sort before trimming so a preferred uploader on a later page isn't cut off.
                        item_cards = _order_by_preferred_uploaders(
                            _load_more_tpdb_item_cards(item_soup), preferred_uploaders)[:set_discovery_limit]
                        for poster_link in item_cards:
                            href = poster_link.get('href')
                            poster_url = _tpdb_absolute_url(href)
                            if not poster_url:
                                continue

                            metadata = _extract_tpdb_card_metadata(poster_link)
                            metadata['source_url'] = candidate['url']
                            set_url = metadata.get('set_url')
                            if set_url and set_url not in discovered_set_lookup:
                                cached_set = cached_sets_by_id.get(str(metadata.get('set_id') or ''))
                                preview_base64 = None
                                if cached_set and cached_set.get('preview_base64'):
                                    preview_base64 = cached_set.get('preview_base64')
                                elif not requested_set_urls:
                                    preview_base64 = _get_tpdb_preview_image(
                                        poster_url,
                                        include_base64,
                                        metadata.get('preview_url'),
                                        preview_image_cache,
                                        max_size=preview_max_size,
                                        quality=preview_quality,
                                    )
                                discovered_set_order[set_url] = len(discovered_set_urls)
                                discovered_set_urls.append(set_url)
                                discovered_set_lookup[set_url] = {
                                    'set_id': metadata.get('set_id'),
                                    'set_url': set_url,
                                    'uploader': metadata.get('uploader') or (cached_set or {}).get('uploader') or 'Unknown',
                                    'preview_url': metadata.get('preview_url') or (cached_set or {}).get('preview_url') or poster_url,
                                    'preview_base64': preview_base64,
                                }
                            if requested_set_urls and set_url not in requested_set_urls:
                                continue
                            if not requested_set_urls and set_url and discovered_set_order.get(set_url, 0) >= max_posters:
                                continue
                            base64_image = _get_tpdb_preview_image(
                                poster_url,
                                include_base64,
                                metadata.get('preview_url'),
                                preview_image_cache,
                                max_size=preview_max_size,
                                quality=preview_quality,
                            )
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
                            elif season_key:
                                remember_unmatched_season(season_key, poster_url, metadata)
                            else:
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
                                logging.info("Checking TPDb poster set for '%s': %s", search_query, set_url)
                                _open_tpdb_page_with_delay(set_url)
                                current_url = selenium_driver.current_url
                                if _is_login_url(current_url):
                                    logging.warning("TPDb session expired on set page for '%s' (%s).", item_title, current_url)
                                    raise TPDBSessionExpired("TPDb session expired while loading set page.")

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
                                    metadata['source_url'] = candidate['url']
                                    if not _tpdb_card_matches_item(metadata, expected_title_norm, expected_year):
                                        continue
                                    season_key = _extract_tpdb_season_key(poster_link)
                                    poster_type = (metadata.get('tpdb_poster_type') or '').lower()
                                    if season_key and season_key in season_by_key:
                                        season = season_by_key[season_key]
                                        base64_image = _get_tpdb_preview_image(
                                            poster_url,
                                            include_base64,
                                            metadata.get('preview_url'),
                                            preview_image_cache,
                                            max_size=preview_max_size,
                                            quality=preview_quality,
                                        )
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
                                        base64_image = _get_tpdb_preview_image(
                                            poster_url,
                                            include_base64,
                                            metadata.get('preview_url'),
                                            preview_image_cache,
                                            max_size=preview_max_size,
                                            quality=preview_quality,
                                        )
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
                                    elif season_key:
                                        remember_unmatched_season(season_key, poster_url, metadata)

                        should_fallback_to_season_pages = season_by_key and (
                            not discovered_set_urls or not group['season_posters']
                        )
                        if should_fallback_to_season_pages:
                            logging.info(
                                "No usable TPDb set season posters found for '%s'; falling back to season-filtered pages.",
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
                                "Checking TPDb season posters for '%s': %s season %s",
                                search_query,
                                candidate['title'],
                                season_param,
                            )
                            _open_tpdb_page_with_delay(season_url)
                            current_url = selenium_driver.current_url
                            if _is_login_url(current_url):
                                logging.warning("TPDb session expired on season page for '%s' (%s).", item_title, current_url)
                                raise TPDBSessionExpired("TPDb session expired while loading season page.")

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
                                metadata['source_url'] = candidate['url']
                                base64_image = _get_tpdb_preview_image(
                                    poster_url,
                                    include_base64,
                                    metadata.get('preview_url'),
                                    preview_image_cache,
                                    max_size=preview_max_size,
                                    quality=preview_quality,
                                )
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
                    logging.warning("TPDb session expired; re-authenticating and retrying once for '%s'.", item_title)
                    setup_selenium_and_login(force=True)
                    continue
                raise
            except TPDBRateLimited:
                if attempt < 2:
                    backoff_sec = 2 + attempt * 2
                    logging.warning("TPDb challenge/rate-limit detected for '%s'; retrying in %ss.", item_title, backoff_sec)
                    time.sleep(backoff_sec)
                    continue
                raise

        groups = sorted(groups, key=lambda group: (-group['covered_season_count'], -group['match_score'], group['source_index']))
        best_group = groups[0] if groups else None
        posters = []
        if best_group:
            posters = best_group['show_posters'][:max_posters]
        if posters:
            logging.info(f"Found {len(posters)} poster links.")
        else:
            logging.warning("Found 0 poster links for '%s'.", item_title)
        return {
            'posters': posters,
            'groups': groups,
            'best_group': best_group,
            'search_query': search_query,
        }
    except Exception:
        logging.exception("Error during TPDb scraping: search_url=%s current_url=%s", search_url, _get_selenium_current_url())
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

def _comparison_title(title, is_collection=False):
    normalized = normalize_title_for_comparison(title)
    if is_collection:
        normalized = re.sub(r'\s*\b(?:collection|saga|anthology)$', '', normalized).strip()
    return normalized


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
            logging.warning(f"Image file not found: {image_path}")
            return False

        # Check if images are identical
        if are_images_identical(item_id, image_path, 'Primary'):
            logging.info(f"Image for item {item_id} is identical to existing.")
            return True

        with open(image_path, 'rb') as f:
            image_data = f.read()

        url = f"{Config.JELLYFIN_URL}/Items/{item_id}/Images/Primary/0"
        headers = get_jellyfin_headers(
            **{
                'Content-Type': get_content_type(image_path),
                'Connection': 'keep-alive',
            }
        )

        # Despite declaring a binary body in its OpenAPI document, Jellyfin 12's
        # ImageController still base64-decodes the request stream.
        encoded_data = base64.b64encode(image_data)
        response = requests.post(url, headers=headers, data=encoded_data, timeout=30)

        if response.status_code in [200, 204]:
            logging.info("Artwork uploaded successfully.")
            return True

        response_detail = (response.text or '').strip()
        detail_suffix = f": {response_detail[:500]}" if response_detail else ''
        logging.warning(f"Failed to upload artwork: {response.status_code}{detail_suffix}")
        return False

    except Exception as e:
        logging.error(f"Error during image upload: {e}")
        return False


def _parse_jellyfin_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


JELLYFIN_PAGE_SIZE = 200
JELLYFIN_MAX_PAGES = 1000


def _jellyfin_get_all_items(url, headers, page_size=JELLYFIN_PAGE_SIZE):
    """Fetch every item for an /Items-style query, following StartIndex/Limit paging.

    Stock Jellyfin returns everything in one response, but compatible servers such as Silo
    cap each page (e.g. at 72 items), so keep requesting until TotalRecordCount is reached.
    """
    items, seen = [], set()
    start = 0
    separator = '&' if '?' in url else '?'
    for _ in range(JELLYFIN_MAX_PAGES):
        response = requests.get(f"{url}{separator}StartIndex={start}&Limit={page_size}", headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        page = data.get('Items') or []
        added = 0
        for item in page:
            item_id = item.get('Id')
            if item_id and item_id in seen:
                continue
            if item_id:
                seen.add(item_id)
            items.append(item)
            added += 1
        start += len(page)
        total = data.get('TotalRecordCount')
        # A short page is not the end: servers may cap Limit below what we asked for.
        if not page or not added or (isinstance(total, int) and start >= total):
            break
    else:
        logging.warning("Stopped paging Jellyfin items after %s pages: %s", JELLYFIN_MAX_PAGES, url)
    return items


def get_jellyfin_collection_children(boxset_id):
    """Return the movies inside a Jellyfin collection (BoxSet)."""
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY or not boxset_id:
        return []
    url = (
        f"{Config.JELLYFIN_URL}/Items?ParentId={boxset_id}&IncludeItemTypes=Movie&Recursive=true"
        "&Fields=Id,Name,ProductionYear,ImageTags,ProviderIds&SortBy=ProductionYear,SortName&SortOrder=Ascending"
    )
    try:
        children = []
        for item in _jellyfin_get_all_items(url, get_jellyfin_headers(Accept="application/json")):
            if item.get('Type', 'Movie') != 'Movie' or not item.get('Id'):
                continue
            tag = (item.get('ImageTags') or {}).get('Primary')
            children.append({
                'id': item['Id'],
                'title': item.get('Name'),
                'year': item.get('ProductionYear'),
                'has_poster': bool(tag),
                'thumbnail_url': (f"{Config.JELLYFIN_URL}/Items/{item['Id']}/Images/Primary?maxWidth=300&quality=85&tag={tag}"
                                  if tag else None),
            })
        return children
    except Exception as e:
        raise RuntimeError(f'Could not fetch movies for Jellyfin collection {boxset_id}') from e


def parse_tpdb_set_posters(html):
    """Parse a TPDb set page into typed poster entries (collection / movie / show / season / ...)."""
    soup = BeautifulSoup(html or '', 'html.parser')
    posters, seen = [], set()
    for poster_link in soup.select(ITEM_POSTER_SELECTOR):
        poster_url = _tpdb_absolute_url(poster_link.get('href'))
        if not poster_url or poster_url in seen:
            continue
        seen.add(poster_url)
        metadata = _extract_tpdb_card_metadata(poster_link)
        title = metadata.get('tpdb_title') or ''
        posters.append({
            'url': poster_url,
            'poster_type': (metadata.get('tpdb_poster_type') or '').lower(),
            'title': title,
            'year': extract_title_year(title),
            'tpdb_poster_id': metadata.get('tpdb_poster_id'),
            'set_id': metadata.get('set_id'),
            'set_url': metadata.get('set_url'),
            'uploader': metadata.get('uploader'),
            'preview_url': metadata.get('preview_url'),
        })
    return posters


def get_tpdb_set_posters(set_url):
    """Load a TPDb set page with the logged-in browser and return its typed posters."""
    for attempt in range(2):
        with selenium_lock:
            if not selenium_driver:
                setup_selenium_and_login()
            try:
                _open_tpdb_page_with_delay(set_url)
                current_url = selenium_driver.current_url
                if _is_login_url(current_url):
                    raise TPDBSessionExpired("TPDb session expired while loading set page.")
                _raise_if_rate_limited(selenium_driver.page_source, current_url, "set_page")
                _wait_for_item_posters_ready(selenium_driver, timeout=15)
                return parse_tpdb_set_posters(selenium_driver.page_source)
            except TPDBSessionExpired:
                if attempt:
                    raise
                logging.warning("TPDb session expired on set page %s; re-authenticating.", set_url)
                setup_selenium_and_login(force=True)
    return []


def match_collection_set(collection_title, children, set_posters):
    """Pair a TPDb set's posters with a Jellyfin collection and its movies.

    Movies match on normalised title + year (exact only). The collection poster is the
    set's collection card whose title matches the Jellyfin collection, or the only one.
    """
    collection_cards = [poster for poster in set_posters if poster['poster_type'] == 'collection']
    wanted = _comparison_title(strip_title_year(collection_title), True)
    collection_poster = next((poster for poster in collection_cards
                              if _comparison_title(strip_title_year(poster['title']), True) == wanted), None)
    if not collection_poster and len({poster['url'] for poster in collection_cards}) == 1:
        collection_poster = collection_cards[0]

    movie_posters = {}
    for poster in set_posters:
        if poster['poster_type'] != 'movie' or not poster['year']:
            continue
        key = (normalize_title_for_comparison(strip_title_year(poster['title'])), poster['year'])
        movie_posters.setdefault(key, poster)

    matches, unmatched_children, used = [], [], set()
    for child in children:
        key = (normalize_title_for_comparison(strip_title_year(child.get('title') or '')),
               str(child['year']) if child.get('year') else None)
        poster = movie_posters.get(key) if key[1] else None
        if poster:
            matches.append({'child': child, 'poster': poster})
            used.add(poster['url'])
        else:
            unmatched_children.append(child)
    return {
        'collection_poster': collection_poster,
        'matches': matches,
        'unmatched_children': unmatched_children,
        'unmatched_posters': [poster for poster in movie_posters.values() if poster['url'] not in used],
    }


def get_jellyfin_seasons(series_id):
    """Return eligible Jellyfin seasons for a Series item, including Specials unless future-dated."""
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY or not series_id:
        return []

    headers = get_jellyfin_headers(Accept="application/json")
    seasons_url = (
        f"{Config.JELLYFIN_URL}/Shows/{series_id}/Seasons"
        "?Fields=Id,Name,IndexNumber,PremiereDate,ImageTags"
    )
    try:
        seasons = []
        now = datetime.utcnow()
        for season in _jellyfin_get_all_items(seasons_url, headers):
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
        raise RuntimeError(f'Could not fetch seasons for Jellyfin series {series_id}') from e


def get_jellyfin_server_info():
    try:
        url = f"{Config.JELLYFIN_URL}/System/Info"
        response = requests.get(url, headers=get_jellyfin_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()
        return {
            'name': data.get('ServerName', 'Jellyfin Server'),
            'version': data.get('Version', ''),
            'id': data.get('Id', ''),
            'connected': True,
        }
    except Exception as e:
        logging.error(f"Error fetching server info: {e}")
        return {'name': 'Jellyfin Server', 'version': '', 'id': '', 'connected': False}


def get_jellyfin_libraries():
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY:
        return []

    try:
        response = requests.get(
            f"{Config.JELLYFIN_URL}/Library/VirtualFolders",
            headers=get_jellyfin_headers(),
            timeout=10,
        )
        response.raise_for_status()
        libraries = []
        for library in response.json():
            library_id = library.get('ItemId')
            library_name = library.get('Name')
            library_type = library.get('CollectionType', '')
            is_collection_library = library_type == 'boxsets' or (library_name or '').strip().lower() == 'collections'
            if is_collection_library and not _include_collections():
                continue
            if is_collection_library:
                library_type = 'boxsets'

            if library_id and library_name:
                libraries.append({
                    'id': library_id,
                    'name': library_name,
                    'type': library_type
                })
        return libraries
    except Exception as e:
        logging.warning(f"Could not fetch Jellyfin libraries: {e}")
        return []


JELLYFIN_TYPE_LABELS = {'Movie': 'Movie', 'Series': 'Series', 'BoxSet': 'Collection'}


def _jellyfin_type_label(item):
    return JELLYFIN_TYPE_LABELS.get(item.get('Type'), 'Series')


def _include_collections():
    return bool(getattr(Config, 'INCLUDE_COLLECTIONS', False))


def get_jellyfin_items(item_type=None, sort_by='name', libraries=None):
    """
    Fetch a list of movies and TV shows from Jellyfin with thumbnail URLs.
    item_type: 'movies', 'series', 'collections', or None for all
    sort_by: 'name', 'year', 'date_added'
    """
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY:
        logging.error("Jellyfin configuration is missing.")
        return []

    items = []
    headers = get_jellyfin_headers(Accept="application/json")
    libraries = libraries if libraries is not None else get_jellyfin_libraries()
    library_names = {library['id']: library['name'] for library in libraries}

    def build_item(item, item_type_label, fallback_library=None):
        ancestor_ids = item.get('AncestorIds') or []
        library_id = item.get('ParentId', '')
        if library_id not in library_names:
            library_id = next((ancestor_id for ancestor_id in ancestor_ids if ancestor_id in library_names), '')
        if not library_id and fallback_library:
            library_id = fallback_library.get('id', '')
        thumbnail_url = None
        if item.get('ImageTags', {}).get('Primary'):
            thumbnail_url = (
                f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary"
                f"?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
            )
        return {
            "id": item.get('Id'),
            "title": item.get('Name'),
            "year": item.get('ProductionYear'),
            "type": item_type_label,
            "thumbnail_url": thumbnail_url,
            "date_created": item.get('DateCreated', ''),
            "season_count": item.get('ChildCount') if item_type_label == 'Series' else None,
            "library_id": library_id,
            "library_name": library_names.get(library_id, fallback_library.get('name', '') if fallback_library else ''),
            'ProviderIds': item.get('ProviderIds', {})
        }

    sort_params = {
        'name': 'SortName',
        'year': 'ProductionYear,SortName',
        'date_added': 'DateCreated'
    }
    sort_by_param = sort_params.get(sort_by, 'SortName')
    sort_order = 'Descending' if sort_by == 'date_added' else 'Ascending'

    def parse_date(date_str):
        return _parse_jellyfin_datetime(date_str) or datetime.min

    try:
        if libraries:
            # BoxSets also appear in movie libraries when "Group movies into collections" is on.
            include_types = {'movies': "Movie", 'series': "Series", 'collections': "BoxSet"}.get(item_type, "Movie,Series,BoxSet")
            seen_ids = set()

            for library in libraries:
                library_types = include_types
                if library.get('type') == 'boxsets':
                    # Only the collections themselves; their movies belong to the movie libraries.
                    if 'BoxSet' not in include_types:
                        continue
                    library_types = "BoxSet"
                library_items_url = (
                    f"{Config.JELLYFIN_URL}/Items"
                    f"?ParentId={library['id']}"
                    f"&IncludeItemTypes={library_types}&Recursive=true"
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,Type,ParentId,ChildCount"
                    f"&SortBy=SortName&SortOrder=Ascending"
                )
                for item in _jellyfin_get_all_items(library_items_url, headers):
                    if item.get('Id') in seen_ids:
                        continue
                    seen_ids.add(item.get('Id'))
                    items.append(build_item(item, _jellyfin_type_label(item), fallback_library=library))

            if sort_by == 'library':
                items.sort(key=lambda x: ((x.get('library_name') or '').lower(), (x.get('title') or '').lower()))
            elif sort_by == 'year':
                items.sort(key=lambda x: ((x.get('year') or 0), (x.get('title') or '').lower()))
            elif sort_by == 'date_added':
                items.sort(key=lambda x: parse_date(x['date_created']), reverse=True)
            else:
                items.sort(key=lambda x: (x.get('title') or '').lower())

            logging.info(f"Total items fetched: {len(items)}")
            return items

        if sort_by == 'date_added':
            logging.debug("Fetching all items for chronological sorting (mixed types).")
            all_items_url = (
                f"{Config.JELLYFIN_URL}/Items"
                f"?IncludeItemTypes=Movie,Series{',BoxSet' if _include_collections() else ''}&Recursive=true"
                f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,Type,ParentId,ChildCount"
                f"&SortBy={sort_by_param}&SortOrder={sort_order}"
            )
            for item in _jellyfin_get_all_items(all_items_url, headers):
                items.append(build_item(item, _jellyfin_type_label(item)))
            # Python-side sort for safety
            items.sort(key=lambda x: parse_date(x['date_created']), reverse=True)

        else:
            # Movies
            if item_type == 'movies' or item_type is None:
                movies_url = (
                    f"{Config.JELLYFIN_URL}/Items"
                    f"?IncludeItemTypes=Movie&Recursive=true"
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,ParentId"
                    f"&SortBy={sort_by_param}&SortOrder={sort_order}"
                )
                for item in _jellyfin_get_all_items(movies_url, headers):
                    items.append(build_item(item, "Movie"))

            # Series
            if item_type == 'series' or item_type is None:
                shows_url = (
                    f"{Config.JELLYFIN_URL}/Items"
                    f"?IncludeItemTypes=Series&Recursive=true"
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,ParentId,ChildCount"
                    f"&SortBy={sort_by_param}&SortOrder={sort_order}"
                )
                for item in _jellyfin_get_all_items(shows_url, headers):
                    items.append(build_item(item, "Series"))

            # Collections
            if item_type == 'collections' or (item_type is None and _include_collections()):
                collections_url = (
                    f"{Config.JELLYFIN_URL}/Items"
                    f"?IncludeItemTypes=BoxSet&Recursive=true"
                    f"&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated,ParentId"
                    f"&SortBy={sort_by_param}&SortOrder={sort_order}"
                )
                for item in _jellyfin_get_all_items(collections_url, headers):
                    items.append(build_item(item, "Collection"))
    except Exception as e:
        raise RuntimeError('Could not load Jellyfin library items') from e

    logging.info(f"Total items fetched: {len(items)}")
    return items
