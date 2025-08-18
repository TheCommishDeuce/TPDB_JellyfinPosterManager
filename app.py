from flask import Flask, render_template, request, jsonify, session, Response
import uuid
import json
import os
import logging
from datetime import datetime
from poster_scraper import *
from config import Config
import threading

app = Flask(__name__)
app.config.from_object(Config)

# # Setup logging
# if not os.path.exists(Config.LOG_DIR):
#     os.makedirs(Config.LOG_DIR)

# logging.basicConfig(
#     level=logging.INFO if not Config.DEBUG else logging.DEBUG,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.FileHandler(f'{Config.LOG_DIR}/app.log'),
#         logging.StreamHandler()
#     ]
# )

# Global storage for session data
user_sessions = {}
selenium_ready_event = threading.Event()

@app.route('/')
def index():
    """Main page showing all Jellyfin items with server info"""
    session_id = session.get('session_id')
    if not session_id:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id

    # Accept 'movies' or 'series' or None
    item_type = request.args.get('type', None)
    # 'name', 'year', 'date_added'
    sort_by = request.args.get('sort', 'name')

    try:
        server_info = get_jellyfin_server_info()
        logging.info(f"Connected to server: {server_info['name']}")

        jellyfin_items = get_jellyfin_items(item_type=item_type, sort_by=sort_by)

        # Store in session
        user_sessions[session_id] = {
            'items': jellyfin_items,
            'selections': {},
            'progress': 0,
            'server_info': server_info
        }

        return render_template('index.html',
                               items=jellyfin_items,
                               server_info=server_info,
                               current_filter=item_type,
                               current_sort=sort_by)

    except Exception as e:
        logging.error(f"Error loading main page: {e}")
        return render_template('index.html',
                               items=[],
                               server_info={'name': 'Jellyfin Server', 'version': '', 'id': ''},
                               error=str(e),
                               current_filter=item_type,
                               current_sort=sort_by)

@app.route('/item/<item_id>/posters')
def get_item_posters(item_id):
    """Get posters for a specific item"""
    if not selenium_ready_event.wait(timeout=30):
        logging.error("Selenium not ready in time for /item/<item_id>/posters")
        return jsonify({'error': 'Backend service (Selenium) is not ready. Please try again in a moment.'}), 503

    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400

    items = user_sessions[session_id]['items']
    item = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404

    try:
        logging.info(f"Searching posters for: {item['title']}")
        posters = search_tpdb_for_posters_multiple(
            item['title'],
            item.get('year'),
            item.get('type'),
            tmdb_id=item.get('ProviderIds', {}).get('Tmdb'),
            max_posters=Config.MAX_POSTERS_PER_ITEM
        )
        return jsonify({'item': item, 'posters': posters})
    except Exception as e:
        logging.error(f"Error getting posters for {item_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/item/<item_id>/select', methods=['POST'])
def select_poster(item_id):
    """User selects a poster for an item (no upload yet)."""
    session_id = session.get('session_id')
    if not session_id or session_id not in user_sessions:
        return jsonify({'error': 'Session not found'}), 400

    data = request.get_json() or {}
    poster_url = data.get('poster_url')
    if not poster_url:
        return jsonify({'error': 'No poster URL provided'}), 400

    user_sessions[session_id]['selections'][item_id] = poster_url
    logging.info(f"Poster selected for item {item_id}: {poster_url}")

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

    selections = user_sessions[session_id]['selections']
    if item_id not in selections:
        return jsonify({'error': 'No poster selected for this item'}), 400

    poster_url = selections[item_id]

    items = user_sessions[session_id]['items']
    item = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found'}), 404

    try:
        os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
        safe_title = "".join(c for c in item['title'] if c.isalnum() or c in " _-").rstrip()
        save_path = os.path.join(Config.TEMP_POSTER_DIR, f"{safe_title}_{item_id}.jpg")

        logging.info(f"Downloading poster for {item['title']}: {poster_url}")

        if download_image_with_cookies(poster_url, save_path):
            logging.info(f"Uploading poster to Jellyfin for {item['title']}")
            success = upload_image_to_jellyfin_improved(item_id, save_path)

            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except Exception:
                pass

            if success:
                return jsonify({'success': True})
            else:
                return jsonify({'error': 'Failed to upload to Jellyfin'}), 500
        else:
            return jsonify({'error': 'Failed to download poster'}), 500

    except Exception as e:
        logging.error(f"Error uploading poster for {item_id}: {e}")
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

    selections = user_sessions[session_id]['selections']
    items = user_sessions[session_id]['items']
    results = []

    logging.info(f"Starting batch upload of {len(selections)} items")

    for item_id, poster_url in selections.items():
        try:
            item = next((i for i in items if i['id'] == item_id), None)
            if not item:
                results.append({'item_id': item_id, 'success': False, 'error': 'Item not found'})
                continue

            os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
            safe_title = "".join(c for c in item['title'] if c.isalnum() or c in " _-").rstrip()
            save_path = os.path.join(Config.TEMP_POSTER_DIR, f"{safe_title}_{item_id}.jpg")

            if download_image_with_cookies(poster_url, save_path):
                success = upload_image_to_jellyfin_improved(item_id, save_path)
                try:
                    if os.path.exists(save_path):
                        os.remove(save_path)
                except Exception:
                    pass

                results.append({
                    'item_id': item_id,
                    'item_title': item['title'],
                    'success': success,
                    'error': None if success else 'Upload failed'
                })
            else:
                results.append({
                    'item_id': item_id,
                    'item_title': item['title'],
                    'success': False,
                    'error': 'Download failed'
                })

        except Exception as e:
            results.append({
                'item_id': item_id,
                'item_title': item.get('title', 'Unknown'),
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
    """Serve TPDB thumbnails with proper headers and caching"""
    thumbnail_url = request.args.get('url')
    if not thumbnail_url or thumbnail_url == 'None':
        return create_placeholder_thumbnail(), 200

    try:
        session_obj = requests.Session()
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
        logging.warning(f"Error fetching TPDB thumbnail {thumbnail_url}: {e}")
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

@app.route('/batch-auto-poster', methods=['POST'])
def batch_auto_poster():
    """
    Automatically get and upload the first poster for items based on filter.
    """
    try:
        data = request.get_json() or {}
        target_filter = data.get('filter', 'no-poster')  # 'all', 'no-poster', 'movies', 'series'

        logging.info(f"Starting batch auto-poster operation with filter: {target_filter}")

        # Ensure Selenium ready (do not teardown per request)
        try:
            if not selenium_driver:
                setup_selenium_and_login()
            logging.info("Selenium/TPDB login ready for auto-batch.")
        except Exception as e:
            logging.error(f"Failed to setup Selenium/login to TPDB: {e}")
            return jsonify({
                'success': False,
                'error': f'Failed to login to TPDB: {str(e)}',
                'results': [],
                'total_items': 0,
                'processed': 0,
                'successful': 0,
                'failed': 0
            }), 500

        # Get all items
        all_items = get_jellyfin_items()

        # Filter items
        if target_filter == 'all':
            target_items = all_items
        elif target_filter == 'no-poster':
            target_items = [item for item in all_items if not item.get('thumbnail_url')]
        elif target_filter == 'movies':
            target_items = [item for item in all_items if item.get('type') == 'Movie']
        elif target_filter == 'series':
            target_items = [item for item in all_items if item.get('type') == 'Series']
        else:
            target_items = []

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

        os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)

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
                        results.append({
                            'item_id': item_id,
                            'item_title': item_title,
                            'success': False,
                            'error': 'Failed to upload to Jellyfin',
                            'poster_url': poster_url
                        })
                        failed_count += 1
                else:
                    results.append({
                        'item_id': item_id,
                        'item_title': item_title,
                        'success': False,
                        'error': 'Failed to download poster',
                        'poster_url': poster_url
                    })
                    failed_count += 1

            except Exception as e:
                logging.error(f"Error processing item {item.get('title', 'Unknown')}: {e}")
                results.append({
                    'item_id': item.get('id', 'Unknown'),
                    'item_title': item.get('title', 'Unknown'),
                    'success': False,
                    'error': str(e),
                    'poster_url': None
                })
                failed_count += 1

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

        data = request.get_json() or {}
        item_id = data.get('item_id')
        poster_url = data.get('poster_url')

        if not item_id or not poster_url:
            return jsonify({'success': False, 'error': 'Missing item_id or poster_url'}), 400

        items = user_sessions.get(session.get('session_id'), {}).get('items', [])
        item = next((i for i in items if i['id'] == item_id), None)
        if not item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404

        os.makedirs(Config.TEMP_POSTER_DIR, exist_ok=True)
        save_path = os.path.join(Config.TEMP_POSTER_DIR, f"manual_{item_id}.jpg")

        if download_image_with_cookies(poster_url, save_path):
            upload_success = upload_image_to_jellyfin_improved(item_id, save_path)
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except Exception as cleanup_error:
                logging.warning(f"Failed to cleanup temp file {save_path}: {cleanup_error}")

            if upload_success:
                return jsonify({'success': True, 'message': 'Poster uploaded successfully'})
            else:
                return jsonify({'success': False, 'error': 'Failed to upload to Jellyfin'}), 500
        else:
            return jsonify({'success': False, 'error': 'Failed to download poster'}), 500

    except Exception as e:
        logging.error(f"Error uploading poster: {e}")
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
        selenium_ready_event.set()

        try:
            server_info = get_jellyfin_server_info()
            logging.info(f"Connected to Jellyfin server: {server_info['name']} (v{server_info.get('version', 'Unknown')})")
        except Exception as e:
            logging.warning(f"Could not connect to Jellyfin server: {e}")

    except Exception as e:
        logging.error(f"Failed to perform background setup: {e}")

if __name__ == '__main__':
    setup_thread = threading.Thread(target=background_setup, daemon=True)
    setup_thread.start()

    try:
        app.run(debug=Config.DEBUG, host='0.0.0.0', port=5000)
    except Exception as e:
        logging.error(f"Failed to start Flask application: {e}")
    finally:
        teardown_selenium()
        logging.info("Application shutdown complete")
