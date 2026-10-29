# FamilySearch Locations Scraper, Cache & Batch Excel Reporter

A high-throughput, production-ready Python application that processes geographic search queries against `https://locations.familysearch.org/en/search`, caches queries and their 1-to-many location results in a local SQLite database (`familysearch_cache.db`), and exports batch Excel (`.xlsx`) reports with timestamped filenames.

---

## Table of Contents
1. [Overview & How It Works](#overview--how-it-works)
2. [End-to-End Step-by-Step Example](#end-to-end-step-by-step-example)
3. [Architecture & Components](#architecture--components)
4. [Dual Fetching Strategy](#dual-fetching-strategy)
5. [Database Schema & Cache-First Design](#database-schema--cache-first-design)
6. [Batch Processing & Excel Reporting](#batch-processing--excel-reporting)
7. [Project File Structure](#project-file-structure)
8. [Installation & Setup](#installation--setup)
9. [CLI Usage & Examples](#cli-usage--examples)
10. [Configuration Reference](#configuration-reference)

---

## Overview & How It Works

This project is built to handle geographic search queries against the FamilySearch Locator system (powered by Yext Search). Each input query contains a place name, latitude, longitude, and radius in miles.

```
                    ┌─────────────────────────┐
                    │    input_queries.csv    │
                    └────────────┬────────────┘
                                 │
                   (Split into batches of N)
                                 ▼
                     ┌───────────────────────┐
                     │  Normalized Signature │
                     │  "qp|lat|lng|radius"   │
                     └───────────┬───────────┘
                                 │
                                 ▼
                      /─────────────────────\
                     <  Exists in SQLite DB? >
                      \─────────────────────/
                            /           \
                 YES (Hit) /             \ NO (Miss)
                          /               \
                         ▼                 ▼
          ┌───────────────────────┐   ┌───────────────────────────┐
          │ Read cached locations │   │ Query Network:            │
          │ from SQLite           │   │ 1. Direct HTTP API (httpx)│
          │ (0 network requests)  │   │ 2. Playwright fallback    │
          └───────────┬───────────┘   └─────────────┬─────────────┘
                      │                             │
                      │                             ▼
                      │               ┌───────────────────────────┐
                      │               │ Save Query & Locations to │
                      │               │ SQLite Cache Database     │
                      │               └─────────────┬─────────────┘
                      │                             │
                      └──────────────┬──────────────┘
                                     │
                                     ▼
                     ┌───────────────────────────────┐
                     │  Export Formatted Batch Excel │
                     │  reports/familysearch_batch_  │
                     │  {batch}_{YYYYMMDD_HHMMSS}    │
                     └───────────────────────────────┘
```

---

## End-to-End Step-by-Step Example

Let's trace a concrete row from [`input_queries.csv`](file:///home/sajibbarua/myProject/family-search-web-scrapping/input_queries.csv):

```csv
qp,lat,lng,r
"Albuquerque, New Mexico, United States",35.088192,-106.650945,50
```

### Step 1: Input Parsing & Signature Generation
The raw inputs are:
- `qp`: `"Albuquerque, New Mexico, United States"`
- `lat`: `35.088192`
- `lng`: `-106.650945`
- `r`: `50` (miles)

The application normalizes whitespace, converts text to lowercase, formats coordinates to 6 decimal places, and rounds radius to an integer:
```python
signature = "albuquerque, new mexico, united states|35.088192|-106.650945|50"
```
This guarantees that queries for the same location always map to the exact same cache key regardless of minor differences in casing or whitespace.

### Step 2: Cache-First SQLite Lookup
The system executes a fast indexed query:
```sql
SELECT id, query_signature, qp, lat, lng, radius, result_count 
FROM search_queries 
WHERE query_signature = 'albuquerque, new mexico, united states|35.088192|-106.650945|50';
```
- **If Found (Cache Hit)**: The query and its linked rows in `location_results` are fetched immediately from SQLite. No external HTTP request is made.
- **If Not Found (Cache Miss)**: The query proceeds to Step 3 for network fetching.

### Step 3: Network Data Fetching (Direct REST API)
The system calls the underlying Yext/FamilySearch Locator API:
- **Endpoint**: `https://prod-cdn.us.yextapis.com/v2/accounts/me/search/vertical/query`
- **Spatial Radius Calculation**: `50 miles * 1609.344 = 80467 meters`.
- **Filters Parameter**:
  ```json
  {
    "builtin.location": {
      "$near": {
        "lat": 35.088192,
        "lng": -106.650945,
        "radius": 80467
      }
    }
  }
  ```
- **Query Parameters**:
  - `experienceKey`: `locator`
  - `api_key`: `0725f078d09ed32336ef9337bd4e188a`
  - `verticalKey`: `locations`
  - `v`: `20220511`
  - `version`: `PRODUCTION`
  - `limit`: `50`

### Step 4: Normalization of Returned Entities
The API returns a JSON array of location objects. The parser normalizes each item:
```json
{
  "location_entity_id": "1961845",
  "location_name": "FamilySearch affiliate",
  "location_type": "Affiliate",
  "street_address": "501 Copper Ave NW, 2nd Floor",
  "city": "Albuquerque",
  "state_region": "NM",
  "postal_code": "87102",
  "country": "US",
  "phone": "+15057685131",
  "website_url": "http://abqlibrary.org/genealogy",
  "res_lat": 35.0860854,
  "res_lng": -106.6531705,
  "distance_miles": 0.19,
  "hours_or_notes": "Tuesday: 10:00-18:00; Wednesday: 10:00-18:00...",
  "raw_json": { ... }
}
```
*Note: Distance is converted from meters (`distanceFromFilter: 310`) to miles (`310 / 1609.344 = 0.19`).*

### Step 5: Transactional Database Insertion
Within an atomic SQLite transaction:
1. Insert parent record into `search_queries`:
   - `id`: `1`
   - `query_signature`: `"albuquerque, new mexico, united states|35.088192|-106.650945|50"`
   - `result_count`: `25`
2. Insert 25 child records into `location_results` referencing `query_id = 1`.
3. If a search returns 0 locations, the query is still stored with `result_count = 0`. On future runs, this prevents making unnecessary external requests for locations that have no results.

### Step 6: Batch Excel Reporting
After completing each batch (e.g., 50 queries):
1. Merges each location record with its parent search parameters (`Search QP`, `Search Lat`, `Search Lng`, `Search Radius`, `Source Status: FETCHED` or `CACHED`).
2. Generates an Excel file with a timestamp down to the second:
   `reports/familysearch_batch_1_20261029_162216.xlsx`
3. Applies openpyxl styling: navy blue header (`#1F4E78`), white bold text, auto-fitted columns, and frozen headers.

---

## Architecture & Components

```
family-search-web-scrapping/
├── config.py             # Global settings, endpoints, timeouts, and headers
├── database.py           # SQLite connection, schema, indexes, and queries
├── scraper_client.py     # Async HTTP client and Playwright fallback scraper
├── exporter.py           # Excel (.xlsx) generation & openpyxl formatting
├── main.py               # Orchestrator & CLI entry point
├── input_queries.csv     # Input geographic search list
├── familysearch_cache.db # SQLite relational cache database
└── reports/              # Timestamped batch Excel output files
```

### Module Responsibilities

| Module | Core Responsibility |
| :--- | :--- |
| [`config.py`](file:///home/sajibbarua/myProject/family-search-web-scrapping/config.py) | Centralizes all configurable settings (`BATCH_SIZE`, `MAX_CONCURRENCY`, `DB_PATH`, `API_BASE_URL`, `DEFAULT_API_KEY`). Can be overridden via environment variables or CLI flags. |
| [`database.py`](file:///home/sajibbarua/myProject/family-search-web-scrapping/database.py) | Manages SQLite schema with foreign keys enabled (`PRAGMA foreign_keys = ON;`). Implements `generate_query_signature()`, `get_cached_query()`, and `save_query_and_results()`. |
| [`scraper_client.py`](file:///home/sajibbarua/myProject/family-search-web-scrapping/scraper_client.py) | Contains `FamilySearchClient` with concurrency limiting (`asyncio.Semaphore`), tenacity exponential backoff retries, auto-discovery of API keys from the search page HTML, and Playwright XHR network interception. |
| [`exporter.py`](file:///home/sajibbarua/myProject/family-search-web-scrapping/exporter.py) | Converts batch query results into a structured pandas DataFrame, merges parent/child records, and writes to `.xlsx` with openpyxl styling. |
| [`main.py`](file:///home/sajibbarua/myProject/family-search-web-scrapping/main.py) | Reads and validates CSV, chunks queries into batches, coordinates cache checks and network fetches, and triggers reports. |

---

## Dual Fetching Strategy

The application provides two complementary data-fetching modes:

### 1. Direct HTTP REST API Mode (Primary & Default)
- **High Speed**: Queries `https://prod-cdn.us.yextapis.com/v2/accounts/me/search/vertical/query` directly via `httpx.AsyncClient`.
- **Concurrency Control**: Bounded by `asyncio.Semaphore(MAX_CONCURRENCY)` to prevent rate limiting.
- **Tenacity Retries**: Automatically retries on network timeouts, HTTP `429` (Rate Limited), and `5xx` Server Errors using exponential backoff.
- **Self-Healing API Keys**: If the API returns HTTP `401` or `403` (expired token), the client scrapes the active `c_searchExperienceAPIKey` from `https://locations.familysearch.org/en/search` and retries the request automatically.

### 2. Playwright Interception Fallback Mode
- Activated by passing the `--force-playwright` flag or automatically when external REST calls are blocked by CDN firewalls.
- Launches a headless Chromium browser instance.
- Navigates to `https://locations.familysearch.org/en/search?qp={qp}&lat={lat}&lng={lng}&r={radius}`.
- Uses `page.expect_response()` to intercept the backend search JSON response as it arrives over the wire.
- Captures responses in **~1.8 seconds per query** without needing to scrape fragile DOM elements.

---

## Database Schema & Cache-First Design

The SQLite database (`familysearch_cache.db`) uses two relational tables:

```sql
-- 1. Parent table: Stores search queries and metadata
CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_signature TEXT UNIQUE NOT NULL,
    qp TEXT,
    lat REAL,
    lng REAL,
    radius INTEGER,
    result_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Child table: Stores locations (1-to-many relationship)
CREATE TABLE IF NOT EXISTS location_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id INTEGER NOT NULL,
    location_entity_id TEXT,
    location_name TEXT,
    location_type TEXT,
    street_address TEXT,
    city TEXT,
    state_region TEXT,
    postal_code TEXT,
    country TEXT,
    phone TEXT,
    website_url TEXT,
    res_lat REAL,
    res_lng REAL,
    distance_miles REAL,
    hours_or_notes TEXT,
    raw_json TEXT,
    FOREIGN KEY (query_id) REFERENCES search_queries(id) ON DELETE CASCADE
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_search_queries_sig ON search_queries(query_signature);
CREATE INDEX IF NOT EXISTS idx_location_results_qid ON location_results(query_id);
```

### Why Store Zero-Result Queries?
If a search query (e.g. in a remote rural area) returns 0 locations, the query is still saved to `search_queries` with `result_count = 0`. On subsequent runs, the system sees that the area was already searched, displays "0 locations found" in the report, and avoids repeating external network calls.

---

## Batch Processing & Excel Reporting

Queries are processed in batches defined by `BATCH_SIZE` (default `50`). At the end of each batch, an Excel report is written to:
`reports/familysearch_batch_{batch_number}_{YYYYMMDD_HHMMSS}.xlsx`

### Generated Excel Columns
1. **Batch Number**: Batch index (1, 2, 3...)
2. **Query Signature**: Normalized lookup key
3. **Search QP**: Original place query text
4. **Search Lat**: Target latitude
5. **Search Lng**: Target longitude
6. **Search Radius (mi)**: Search radius in miles
7. **Source Status**: `FETCHED` (newly queried) or `CACHED` (retrieved from database)
8. **Location Entity ID**: Yext/FamilySearch entity identifier
9. **Location Name**: e.g., "FamilySearch affiliate", "FamilySearch Center"
10. **Location Type**: e.g., "Affiliate", "Center"
11. **Street Address**: Street lines combined
12. **City**: City name
13. **State/Region**: State / Province code
14. **Postal Code**: ZIP / Postal code
15. **Country**: ISO Country code
16. **Phone**: Contact telephone number
17. **Website URL**: Official website or affiliate catalog URL
18. **Result Lat**: Entity latitude
19. **Result Lng**: Entity longitude
20. **Distance (Miles)**: Distance from search point in miles
21. **Hours / Notes**: Structured opening hours and location notes

---

## Installation & Setup

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.12)
- Linux / macOS / Windows

### 2. Environment Setup
```bash
# Clone or navigate to the repository
cd family-search-web-scrapping

# Create a virtual environment
python3 -m venv .venv

# Activate the virtual environment
source .venv/bin/activate       # On Linux/macOS
# .venv\Scripts\activate        # On Windows

# Install Python requirements
pip install -r requirements.txt

# Install Playwright Chromium browser binaries
python -m playwright install chromium
```

---

## CLI Usage & Examples

### Basic Run
Run the scraper using the default `input_queries.csv` with a batch size of 50:
```bash
python main.py
```

### Specify Batch Size
Chunk processing and generate an Excel report after every 10 queries:
```bash
python main.py --batch-size 10
```

### Force Playwright Browser Interception
Use headless Playwright browser interception instead of direct REST API calls:
```bash
python main.py --force-playwright
```

### Custom Input and Output Paths
```bash
python main.py --input data/my_queries.csv --reports-dir output_excel/ --db-path cache/custom.db
```

### Verbose Debug Logging
Inspect detailed HTTP requests, lat/lng conversions, and SQL operations:
```bash
python main.py --verbose
```

### All CLI Options
```
options:
  -h, --help            Show this help message and exit
  --input, -i PATH      Path to input queries CSV (default: input_queries.csv)
  --batch-size, -b INT  Number of queries per batch / Excel export (default: 50)
  --concurrency, -c INT Maximum concurrent network requests (default: 5)
  --db-path PATH        Path to SQLite cache database (default: familysearch_cache.db)
  --reports-dir PATH    Output directory for Excel reports (default: reports)
  --force-playwright    Force Playwright browser interception mode
  --verbose, -v         Enable detailed debug logging
```

---

## Configuration Reference

You can configure the application via CLI arguments, [`config.py`](file:///home/sajibbarua/myProject/family-search-web-scrapping/config.py), or environment variables:

| Environment Variable | Default Value | Description |
| :--- | :--- | :--- |
| `FS_BATCH_SIZE` | `50` | Number of queries processed per Excel report |
| `FS_MAX_CONCURRENCY` | `5` | Maximum concurrent async HTTP requests |
| `FS_DB_PATH` | `familysearch_cache.db` | Local SQLite cache database file path |
| `FS_REPORTS_DIR` | `reports` | Target directory for generated Excel reports |
| `FS_API_BASE_URL` | `https://prod-cdn.us.yextapis.com/v2/accounts/me/search/vertical/query` | Backend Yext / FamilySearch Search API endpoint |
| `FS_API_KEY` | `0725f078d09ed32336ef9337bd4e188a` | Active API key for FamilySearch locator |
| `FS_REQUEST_TIMEOUT` | `30.0` | Timeout per HTTP request (in seconds) |
| `FS_MAX_RETRIES` | `5` | Maximum tenacity retry attempts on 429/5xx errors |
