# FamilySearch Locations Scraper, Cache & Batch Excel Reporter

A high-throughput, production-ready Python application that processes geographic search queries against `https://locations.familysearch.org/en/search`, caches queries and their 1-to-many location results in a local SQLite database (`familysearch_cache.db`), and exports batch Excel (`.xlsx`) reports with timestamped filenames.

---

## Architecture & Features

1. **Dual Fetching Strategy**:
   - **Direct HTTP API Mode (Primary)**: High-speed async queries using `httpx.AsyncClient` with `asyncio.Semaphore` for strict concurrency control and `tenacity` exponential backoff retries on `429` / `5xx` errors.
   - **Playwright Interception Fallback Mode**: Headless Chromium browser that intercepts the network XHR/Fetch responses from the FamilySearch/Yext locator engine and auto-extracts dynamic API keys and location data.
2. **Deterministic Query Signature & Cache-First Lookup**:
   - Computes normalized signature: `normalized_qp|lat_6f|lng_6f|radius_int`.
   - Before any external network call, checks the SQLite database `search_queries` table. If cached, reuses the saved location records immediately, reducing external traffic to zero.
   - Saves zero-result queries (`result_count = 0`) to prevent redundant lookups on empty areas.
3. **Relational Database (`SQLite3` with `aiosqlite`)**:
   - `search_queries`: Parent table storing input parameters, normalized signature, result counts, and timestamps.
   - `location_results`: Child table linked via foreign key (`query_id`) containing structured location attributes (address, coordinates, distance in miles, phone, website, hours/notes, and raw JSON).
4. **Timestamped Batch Excel Reports (`pandas` + `openpyxl`)**:
   - Chunks input queries into configurable batch sizes (`BATCH_SIZE`, default: 50).
   - Generates an Excel report upon completing each batch:
     `reports/familysearch_batch_{batch_number}_{YYYYMMDD_HHMMSS}.xlsx`
   - Formats columns, applies navy header styling, gridlines, auto-fitted column widths, and freezes header rows.

---

## Project Structure

```
├── config.py             # Configurable constants & environment variables
├── database.py           # SQLite connection manager, schema initialization & transactional queries
├── scraper_client.py     # Async client with Direct HTTP API & Playwright interception fallback
├── exporter.py           # Batch Excel generator and openpyxl styling formatter
├── main.py               # Main CLI runner orchestrating the batch pipeline
├── requirements.txt      # Project dependencies
├── input_queries.csv     # Sample input queries CSV
├── familysearch_cache.db # SQLite cache database
└── reports/              # Output directory for generated batch Excel reports
```

---

## Installation & Setup

1. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Install Playwright Chromium browser**:
   ```bash
   python -m playwright install chromium
   ```

---

## Usage

### Run with Defaults
Processes `input_queries.csv` in batches and saves Excel reports to `reports/`:
```bash
python main.py
```

### Command-Line Arguments
```
options:
  -h, --help            Show this help message and exit
  --input, -i PATH      Path to input queries CSV (default: input_queries.csv)
  --batch-size, -b INT  Number of queries per batch / Excel export (default: 50)
  --concurrency, -c INT Maximum concurrent requests (default: 5)
  --db-path PATH        Path to SQLite cache database (default: familysearch_cache.db)
  --reports-dir PATH    Output directory for Excel reports (default: reports)
  --force-playwright    Force Playwright browser interception mode for all queries
  --verbose, -v         Enable debug logging
```

### Examples

- **Custom Batch Size**:
  ```bash
  python main.py --batch-size 2
  ```

- **Run with Playwright Interception**:
  ```bash
  python main.py --force-playwright
  ```

- **Custom Input CSV and Reports Directory**:
  ```bash
  python main.py --input /path/to/custom_queries.csv --reports-dir custom_reports/
  ```

---

## Database Schema

```sql
CREATE TABLE search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_signature TEXT UNIQUE NOT NULL,
    qp TEXT,
    lat REAL,
    lng REAL,
    radius INTEGER,
    result_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE location_results (
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
```
# family-search-web-scrapping
