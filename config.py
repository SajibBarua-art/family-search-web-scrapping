"""
Configuration module for FamilySearch Locations Scraper & Caching System.
"""
from pathlib import Path
import os

# Project root directory
BASE_DIR = Path(__file__).resolve().parent

# Database configuration
DB_PATH = Path(os.getenv("FS_DB_PATH", BASE_DIR / "familysearch_cache.db"))

# Export reports directory
REPORTS_DIR = Path(os.getenv("FS_REPORTS_DIR", BASE_DIR / "reports"))

# Batch processing & concurrency settings
BATCH_SIZE = int(os.getenv("FS_BATCH_SIZE", "50"))
MAX_CONCURRENCY = int(os.getenv("FS_MAX_CONCURRENCY", "5"))

# FamilySearch Locator / Yext API endpoints & defaults
SEARCH_PAGE_URL = "https://locations.familysearch.org/en/search"
API_BASE_URL = os.getenv(
    "FS_API_BASE_URL", 
    "https://prod-cdn.us.yextapis.com/v2/accounts/me/search/vertical/query"
)

# Default Yext API parameters discovered from FamilySearch frontend
DEFAULT_API_KEY = os.getenv("FS_API_KEY", "0725f078d09ed32336ef9337bd4e188a")
EXPERIENCE_KEY = os.getenv("FS_EXPERIENCE_KEY", "locator")
VERTICAL_KEY = os.getenv("FS_VERTICAL_KEY", "locations")
API_VERSION = os.getenv("FS_API_VERSION", "20220511")
EXPERIENCE_VERSION = os.getenv("FS_EXPERIENCE_VERSION", "PRODUCTION")
DEFAULT_LOCALE = os.getenv("FS_LOCALE", "en")

# Network & Retry settings
REQUEST_TIMEOUT = float(os.getenv("FS_REQUEST_TIMEOUT", "30.0"))
MAX_RETRIES = int(os.getenv("FS_MAX_RETRIES", "5"))
BACKOFF_MIN = float(os.getenv("FS_BACKOFF_MIN", "1.0"))
BACKOFF_MAX = float(os.getenv("FS_BACKOFF_MAX", "10.0"))

# HTTP Client Headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://locations.familysearch.org",
    "Referer": "https://locations.familysearch.org/en/search",
}
