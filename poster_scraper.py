import requests
import json
from io import BytesIO
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import os
import hashlib
import base64
from urllib.parse import quote_plus
from config import Config
import logging

# Global Selenium driver
selenium_driver = None

def setup_selenium_and_login():
    global selenium_driver
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    selenium_driver = webdriver.Chrome(options=chrome_options)

    # Login to TPDB
    selenium_driver.get("https://theposterdb.com/login")
    time.sleep(2)
    email_input = selenium_driver.find_element(By.NAME, "login")
    password_input = selenium_driver.find_element(By.NAME, "password")
    email_input.send_keys(Config.TPDB_EMAIL)
    password_input.send_keys(Config.TPDB_PASSWORD)
    password_input.send_keys(Keys.RETURN)
    time.sleep(5)  # Wait for login
    logging.info("Selenium initialized successfully")

def teardown_selenium():
    global selenium_driver
    if selenium_driver:
        selenium_driver.quit()
        selenium_driver = None

def get_selenium_cookies_as_dict():
    global selenium_driver
    cookies = selenium_driver.get_cookies()
    return {cookie['name']: cookie['value'] for cookie in cookies}

def download_image_with_cookies(url, save_path):
    session = requests.Session()
    session.cookies.update(get_selenium_cookies_as_dict())
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })
    response = session.get(url, stream=True)
    if response.status_code == 200:
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)
        print(f"Saved image to {save_path}")
        return True
    else:
        print(f"Failed to download image from {url} (status {response.status_code})")
        return False

def get_content_type(file_path):
    """Get content type based on file extension"""
    ext = file_path.split('.')[-1].lower()
    return {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp'
    }.get(ext, 'application/octet-stream')

def calculate_hash(data):
    """Calculate a simple hash of image data"""
    return hashlib.md5(data).hexdigest()

def get_local_image_hash(image_path):
    """Get hash of a local image file"""
    try:
        if not os.path.exists(image_path):
            return None
        with open(image_path, 'rb') as f:
            data = f.read()
            return calculate_hash(data)
    except Exception as e:
        print(f"Error calculating hash for {image_path}: {str(e)}")
        return None

def get_jellyfin_image_hash(item_id, image_type='Primary', index=0):
    """Get hash of the current image on Jellyfin server"""
    try:
        url = f"{Config.JELLYFIN_URL}/Items/{item_id}/Images/{image_type}/{index}"
        headers = {'X-Emby-Token': Config.JELLYFIN_API_KEY}
        
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            return None  # Image doesn't exist
        
        response.raise_for_status()
        image_data = response.content
        return calculate_hash(image_data)
    except Exception as e:
        print(f"Error getting image hash from Jellyfin: {str(e)}")
        return None

def are_images_identical(item_id, image_path, image_type='Primary'):
    """Compare if the local image is identical to the one on Jellyfin"""
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
    """Download image and convert to base64 for embedding"""
    try:
        session = requests.Session()
        
        # Get cookies from Selenium driver
        if selenium_driver:
            selenium_cookies = selenium_driver.get_cookies()
            for cookie in selenium_cookies:
                session.cookies.set(cookie['name'], cookie['value'])
        
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://theposterdb.com/",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        })
        
        print(f"Converting image to base64: {image_url}")
        response = session.get(image_url, timeout=15)
        response.raise_for_status()
        
        # Convert to base64
        image_data = base64.b64encode(response.content).decode('utf-8')
        content_type = response.headers.get('content-type', 'image/jpeg')
        
        # Create data URL
        data_url = f"data:{content_type};base64,{image_data}"
        print(f"Successfully converted image to base64 (size: {len(image_data)} chars)")
        
        return data_url
        
    except Exception as e:
        print(f"Error converting image to base64: {e}")
        return None


def search_tpdb_for_posters_multiple(item_title, item_year=None, item_type=None, tmdb_id=None, max_posters=18):
    """
    Modified version that returns up to 12 poster URLs with base64 data
    """
    global selenium_driver
    poster_data = []


    type = None
    if item_type == "Movie":
        type = "movie"
    elif item_type == "Series":
        type = "tv"  # TMDB uses 'tv' for series

    search_query = item_title  # Initialize with Jellyfin title as a fallback

    # Step 1: Use TMDB ID to get the official title for ThePosterDB search if available
    if tmdb_id and type:
        try:
            # Construct TMDB API URL using the correct type
            tmdb_response = requests.get(f"https://api.themoviedb.org/3/{type}/{tmdb_id}?api_key={Config.TMDB_API_KEY}&language=en-US")
            tmdb_response.raise_for_status()
            tmdb_data = tmdb_response.json()

            if type == "tv":
                tmdb_title = tmdb_data.get("name")
                year = tmdb_data.get("first_air_date")[:4]
            else:  # movie
                tmdb_title = tmdb_data.get("title")
                year = tmdb_data.get("release_date")[:4]

            if tmdb_title:
                search_query = f'{tmdb_title} ({year})'
                logging.info(f"Using TMDB official title for TPDB search: {search_query} (from TMDB ID: {tmdb_id})")
            else:
                logging.warning(
                    f"TMDB data for {item_title} (ID: {tmdb_id}, Type: {item_type}) did not contain a 'title' or 'name'. Falling back to Jellyfin title.")

        except Exception as e:
            logging.warning(
                f"Failed to fetch TMDB title for {item_title} (ID: {tmdb_id}, Type: {item_type}): {e}. Falling back to Jellyfin title for search.")

    else:
        logging.info(f"No TMDB ID or type information provided. Using Jellyfin title '{item_title}' for TPDB search.")

    logging.info(f"Searching for {max_posters} posters for TPDB query: '{search_query}'")

    # Step 2: Properly encode the search query for URL
    encoded_query = quote_plus(search_query)

    # Build search URL with properly encoded query
    search_url = Config.TPDB_SEARCH_URL_TEMPLATE.format(query=encoded_query)

    if item_type == "Movie":
        search_url += "&section=movies"
    elif item_type == "Series":
        search_url += "&section=shows"

    logging.info(f"TPDB search URL: {search_url}")

    try:
        # Step 3: Search TPDB for the item title
        selenium_driver.get(search_url)
        time.sleep(2)
        soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')

        # Step 4: Find first 5 search result links
        search_result_links = soup.find_all("a", class_="btn btn-dark-lighter flex-grow-1 text-truncate py-2 text-left position-relative")
        
        if not search_result_links:
            print(f"No search results found for '{search_query}'.")
            return []

        # Step 5: Find best match (using existing logic)
        best_match = None
        best_match_score = 0

        for i, link in enumerate(search_result_links):
            try:
                result_title = link.get_text(strip=True)
                title_element = link.find(class_="text-truncate") or link.find("span") or link
                if title_element:
                    result_title = title_element.get_text(strip=True)

                match_score = calculate_title_match_score(search_query, result_title)

                if match_score > best_match_score:
                    best_match_score = match_score
                    best_match = link

            except Exception as e:
                continue

        if best_match and best_match_score >= 0.8:
            selected_link = best_match
        elif search_result_links:
            selected_link = search_result_links[0]
        else:
            return []

        # Step 6: Get the URL from the selected result
        item_page_path = selected_link.get('href')
        if not item_page_path:
            return []

        if item_page_path.startswith('http'):
            target_item_page_url = item_page_path
        elif item_page_path.startswith('/'):
            target_item_page_url = Config.TPDB_BASE_URL + item_page_path
        else:
            return []

        # Step 7: Go to the item page and scrape UP TO max_posters poster links
        selenium_driver.get(target_item_page_url)
        time.sleep(2)
        item_soup = BeautifulSoup(selenium_driver.page_source, 'html.parser')

        # Find up to max_posters poster links
        poster_links = item_soup.find_all(
            "a",
            class_="bg-transparent border-0 text-white",
            href=True
        )[:max_posters]

        print(f"Found {len(poster_links)} poster links, converting to base64...")

        for i, poster_link in enumerate(poster_links):
            href = poster_link['href']
            if href.startswith('http'):
                poster_url = href
            elif href.startswith('/'):
                poster_url = Config.TPDB_BASE_URL + href
            else:
                continue

            # Get poster metadata
            poster_info = extract_poster_metadata(poster_link)

            # Convert to base64 for embedding
            print(f"Processing poster {i + 1}/{len(poster_links)}: {poster_info.get('title', f'Poster {i + 1}')}")
            base64_image = get_image_as_base64(poster_url)
            
            poster_data.append({
                'id': i + 1,
                'url': poster_url,  # Keep original URL for upload
                'base64': base64_image,  # Add base64 version for display
                'title': poster_info.get('title', f'Poster {i + 1}'),
                'uploader': poster_info.get('uploader', 'Unknown'),
                'likes': poster_info.get('likes', 0)
            })

        print(f"Successfully processed {len(poster_data)} posters with base64 data")

    except Exception as e:
        print(f"Error during TPDB scraping: {e}")

    return poster_data

def extract_poster_metadata(poster_element):
    """Extract additional metadata from poster element"""
    try:
        # Try to find poster title, uploader, likes, etc.
        # This depends on TPDB's actual HTML structure
        title_elem = poster_element.find('title') or poster_element.get('title', '')
        
        return {
            'title': title_elem if isinstance(title_elem, str) else 'Poster',
            'uploader': 'Unknown',
            'likes': 0
        }
    except:
        return {
            'title': 'Poster',
            'uploader': 'Unknown', 
            'likes': 0
        }

def calculate_title_match_score(expected_title, result_title):
    """Calculate similarity score between expected title and search result title."""
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

    common_words = expected_words.intersection(result_words)
    similarity = len(common_words) / max(len(expected_words), len(result_words))

    return similarity

def normalize_title_for_comparison(title):
    """Normalize title for comparison"""
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
    
    normalized = re.sub(r'[^\w\s$$]', ' ', normalized)
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
    """Get Jellyfin server information including name"""
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
        print(f"Error fetching server info: {e}")
        return {'name': 'Jellyfin Server', 'version': '', 'id': ''}
def get_jellyfin_items(item_type=None, sort_by='name'):
    """Fetches a list of movies and TV shows from the Jellyfin server with thumbnail URLs."""
    if not Config.JELLYFIN_URL or not Config.JELLYFIN_API_KEY:
        print("Error: Jellyfin configuration is missing.")
        return []

    items = []
    headers = {
        "X-Emby-Token": Config.JELLYFIN_API_KEY,
        "Accept": "application/json",
    }

    # Build sort parameter for Jellyfin API
    sort_params = {
        'name': 'SortName',
        'year': 'ProductionYear,SortName',
        'date_added': 'DateCreated'
    }
    sort_by_param = sort_params.get(sort_by, 'SortName')
    
    # Set sort order - descending for date_added (newest first), ascending for others
    sort_order = 'Descending' if sort_by == 'date_added' else 'Ascending'

    try:
        # For date_added sorting, we want to mix movies and series chronologically
        # So we'll fetch both types and sort them together afterward
        if sort_by == 'date_added':
            print(f"Fetching all items for chronological sorting...")
            
            # Fetch movies and series in one combined request if no filter is applied
            if item_type is None:
                # Get both movies and series in one API call
                all_items_url = f"{Config.JELLYFIN_URL}/Items?IncludeItemTypes=Movie,Series&Recursive=true&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated&SortBy={sort_by_param}&SortOrder={sort_order}"
                response = requests.get(all_items_url, headers=headers, timeout=10)
                response.raise_for_status()
                all_data = response.json()
                
                if 'Items' in all_data:
                    for item in all_data['Items']:
                        thumbnail_url = None
                        if item.get('ImageTags', {}).get('Primary'):
                            thumbnail_url = f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
                        
                        # Determine type based on Jellyfin's Type field
                        item_type_name = "Movie" if item.get('Type') == 'Movie' else "Series"
                        
                        items.append({
                            "id": item.get('Id'),
                            "title": item.get('Name'),
                            "year": item.get('ProductionYear'),
                            "type": item_type_name,
                            "thumbnail_url": thumbnail_url,
                            "date_created": item.get('DateCreated', ''),
                            'ProviderIds': item.get('ProviderIds', {})
                        })
                
                print(f"Successfully fetched {len(items)} items (mixed movies and series).")
                
            else:
                # If filtering by specific type, still use the filtered approach
                item_types = [item_type] if item_type else ['movies', 'series']
                
                for current_type in item_types:
                    jellyfin_type = 'Movie' if current_type == 'movies' else 'Series'
                    type_url = f"{Config.JELLYFIN_URL}/Items?IncludeItemTypes={jellyfin_type}&Recursive=true&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated&SortBy={sort_by_param}&SortOrder={sort_order}"
                    response = requests.get(type_url, headers=headers, timeout=10)
                    response.raise_for_status()
                    type_data = response.json()
                    
                    if 'Items' in type_data:
                        for item in type_data['Items']:
                            thumbnail_url = None
                            if item.get('ImageTags', {}).get('Primary'):
                                thumbnail_url = f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
                            
                            item_type_name = "Movie" if current_type == 'movies' else "Series"
                            
                            items.append({
                                "id": item.get('Id'),
                                "title": item.get('Name'),
                                "year": item.get('ProductionYear'),
                                "type": item_type_name,
                                "thumbnail_url": thumbnail_url,
                                "date_created": item.get('DateCreated', ''),
                                'ProviderIds': item.get('ProviderIds', {})
                            })
                
                # Sort the combined results by date_created in Python to ensure proper mixing
                from datetime import datetime
                
                def parse_date(date_str):
                    if not date_str:
                        return datetime.min
                    try:
                        # Handle Jellyfin's ISO format
                        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    except:
                        return datetime.min
                
                items.sort(key=lambda x: parse_date(x['date_created']), reverse=True)
                print(f"Successfully fetched and sorted {len(items)} items by date added.")
        
        else:
            # For other sorting methods, keep movies and series separate as before
            if item_type == 'movies' or item_type is None:
                print(f"Fetching movies from Jellyfin (sorted by {sort_by})...")
                movies_url = f"{Config.JELLYFIN_URL}/Items?IncludeItemTypes=Movie&Recursive=true&Fields=Id,Name,ProductionYear,Path,ImageTags,ProviderIds,DateCreated&SortBy={sort_by_param}&SortOrder={sort_order}"
                response = requests.get(movies_url, headers=headers, timeout=10)
                response.raise_for_status()
                movies_data = response.json()

                if 'Items' in movies_data:
                    for item in movies_data['Items']:
                        thumbnail_url = None
                        if item.get('ImageTags', {}).get('Primary'):
                            thumbnail_url = f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"
                        
                        items.append({
                            "id": item.get('Id'),
                            "title": item.get('Name'),
                            "year": item.get('ProductionYear'),
                            "type": "Movie",
                            "thumbnail_url": thumbnail_url,
                            "date_created": item.get('DateCreated', ''),
                            'ProviderIds': item.get('ProviderIds', {})
                        })
                    print(f"Successfully fetched {len(movies_data['Items'])} movies.")

            if item_type == 'series' or item_type is None:
                shows_url = f"{Config.JELLYFIN_URL}/Items?IncludeItemTypes=Series&Recursive=true&Fields=Id,Name,ProductionYear,Path,ImageTags,DateCreated,ProviderIds&SortBy={sort_by_param}&SortOrder={sort_order}"
                response = requests.get(shows_url, headers=headers, timeout=10)
                response.raise_for_status()
                shows_data = response.json()

                if 'Items' in shows_data:
                    for item in shows_data['Items']:
                        thumbnail_url = None
                        if item.get('ImageTags', {}).get('Primary'):
                            thumbnail_url = f"{Config.JELLYFIN_URL}/Items/{item.get('Id')}/Images/Primary?maxWidth=300&quality=85&tag={item['ImageTags']['Primary']}"

                        items.append({
                            "id": item.get('Id'),
                            "title": item.get('Name'),
                            "year": item.get('ProductionYear'),
                            "type": "Series",
                            "thumbnail_url": thumbnail_url,
                            "date_created": item.get('DateCreated', ''),
                            'ProviderIds': item.get('ProviderIds', {})
                        })
                    print(f"Successfully fetched {len(shows_data['Items'])} TV shows.")
    except Exception as e:
        print(f"Error fetching items from Jellyfin: {e}")
        return []

    print(f"Total items fetched: {len(items)} (Movies + TV Shows)")
    return items