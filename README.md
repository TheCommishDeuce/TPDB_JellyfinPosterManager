# Jellyfin Poster Manager

A modern web application for automatically finding and uploading high-quality posters to your Jellyfin media server from ThePosterDB.

![Jellyfin Poster Manager](https://img.shields.io/badge/Jellyfin-Poster%20Manager-blue?style=for-the-badge&logo=jellyfin)
![Python](https://img.shields.io/badge/Python-3.8+-green?style=for-the-badge&logo=python)
![Flask](https://img.shields.io/badge/Flask-2.0+-red?style=for-the-badge&logo=flask)
![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-purple?style=for-the-badge&logo=bootstrap)

## 🎬 Features

### 🔍 **Smart Poster Discovery**
- Automatically searches ThePosterDB for high-quality movie and TV series posters
- Intelligent matching using title, year, and alternative titles
- Supports both movies and TV series

### 🚀 **Batch Operations**
- **Auto-Get Posters**: Automatically find and upload posters for multiple items
- Filter by content type (Movies, TV Series, or All)
- Process only items without existing posters or replace all
- Real-time progress tracking with detailed results

### 🎨 **Manual Selection**
- Browse multiple poster options for each item
- High-quality preview images
- One-click poster upload and replacement

### 📱 **Modern Interface**
- Responsive Bootstrap 5 design
- Works on desktop, tablet, and mobile devices
- Real-time filtering and sorting
- Visual progress indicators

### 🔧 **Advanced Features**
- Automatic image format conversion (WebP/AVIF → JPEG)
- Smart error handling and retry logic
- Comprehensive logging and debugging

## 📋 Requirements

- **Python 3.8+**
- **Jellyfin Server** (any recent version)
- **ThePosterDB Credentials** (free registration required)
- **Network access** to both Jellyfin server and ThePosterDB

## 🚀 Quick Start

### 1. Clone the Repository
### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configuration
Rename config_example.py to config.py in the project root and update with your config:

```env
# Jellyfin Configuration
JELLYFIN_URL = ""
JELLYFIN_API_KEY = ""
JELLYFIN_USER_ID = ""

# TPDB Configuration
TPDB_EMAIL = ""
TPDB_PASSWORD = ""

# Application Settings
MAX_POSTERS_PER_ITEM = 18
TEMP_POSTER_DIR = "temp_posters"
LOG_DIR = "logs"
```

### 4. Run the Application
```bash
python app.py
```

Visit `http://localhost:5000` in your web browser.

## ⚙️ Configuration Guide

### Getting Your Jellyfin API Key

1. Log into your Jellyfin web interface
2. Go to **Dashboard** → **API Keys**
3. Click **"+"** to create a new API key
4. Give it a name (e.g., "Poster Manager")
5. Copy the generated API key

## 🎯 Usage Guide

### Auto-Get Posters (Recommended)

1. Click **"Auto-Get Posters"** button
2. Choose your filter option:
   - **Items Without Posters**: Only process items missing artwork (recommended)
   - **All Items**: Replace all existing posters
   - **Movies Only**: Process only movie items
   - **TV Series Only**: Process only TV series items
3. Wait for the process to complete
4. Review the results summary

### Manual Poster Selection

1. Click **"Find Posters"** on any item
2. Browse the available poster options
3. Click on your preferred poster to upload it
4. The poster will be automatically uploaded to Jellyfin

### Filtering and Sorting

- Use the **All/Movies/Series** buttons to filter content
- Use the **Sort by** dropdown to organize items by:
  - Name (A-Z)
  - Year
  - Recently Added

### Logging Configuration

Logs are written to `logs/app.log` by default. You can adjust logging levels:

```python
import logging
logging.getLogger().setLevel(logging.DEBUG)  # For verbose logging
```


### Debug Mode

Enable debug mode for detailed logging:

```env
FLASK_DEBUG=True
FLASK_ENV=development
```

## 🙏 Acknowledgments

- **[Jellyfin](https://jellyfin.org/)** - The amazing open-source media server
- **[ThePosterDB](https://theposterdb.com/)** - High-quality movie and TV posters
- **[Bootstrap](https://getbootstrap.com/)** - Beautiful responsive UI framework

---

**Made with ❤️ for the Jellyfin community**

*Star this repository if you find it useful!* ⭐
