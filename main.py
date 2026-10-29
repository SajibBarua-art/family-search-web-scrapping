"""
FamilySearch Locations Scraper & Caching Pipeline.
Main CLI entry point for batch processing geographic search queries.
"""
import argparse
import asyncio
from datetime import datetime
import logging
from pathlib import Path
import sys
from typing import List, Dict, Any, Optional
import aiosqlite
import pandas as pd

from config import (
    BATCH_SIZE,
    MAX_CONCURRENCY,
    DB_PATH,
    REPORTS_DIR,
)
from database import (
    init_db,
    generate_query_signature,
    get_cached_query,
    save_query_and_results,
)
from scraper_client import FamilySearchClient
from exporter import export_batch_report


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging format and level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def resolve_column_name(columns: List[str], candidate_names: List[str]) -> Optional[str]:
    """Find the first column matching any candidate name case-insensitively."""
    col_map = {str(c).strip().lower(): c for c in columns}
    for candidate in candidate_names:
        cand_lower = candidate.strip().lower()
        if cand_lower in col_map:
            return col_map[cand_lower]
    return None


def load_input_queries(file_path: Path) -> List[Dict[str, Any]]:
    """
    Load and validate input search queries from an Excel (.xlsx, .xls) or CSV file.
    Supports queries with ONLY 'qp' (place name) as well as full 'qp, lat, lng, r'.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Input file not found at: {file_path}")

    # Load from Excel or CSV based on file extension
    suffix = file_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(file_path)
    else:
        df = pd.read_csv(file_path)

    if df.empty:
        logging.warning("Input file is empty: %s", file_path)
        return []

    columns = [str(c) for c in df.columns]

    # Flexible detection of 'qp' column (e.g. qp, query_place, place, search_query, location, address)
    qp_candidates = ["qp", "query_place", "place", "search_query", "query", "location", "address", "city"]
    qp_col = resolve_column_name(columns, qp_candidates)

    if not qp_col:
        # If single column or first column contains text, use it as qp
        if len(columns) == 1:
            qp_col = columns[0]
        else:
            raise ValueError(
                f"Could not identify a query place column in {columns}. "
                f"Expected one of: {qp_candidates}"
            )

    # Optional coordinate & radius columns
    lat_col = resolve_column_name(columns, ["lat", "latitude"])
    lng_col = resolve_column_name(columns, ["lng", "lon", "long", "longitude"])
    radius_col = resolve_column_name(columns, ["r", "radius", "search_radius"])

    queries: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        try:
            qp_val = row[qp_col]
            if pd.isna(qp_val) or not str(qp_val).strip():
                continue
            qp = str(qp_val).strip()

            lat = None
            if lat_col and not pd.isna(row[lat_col]):
                try:
                    lat = float(row[lat_col])
                except (ValueError, TypeError):
                    lat = None

            lng = None
            if lng_col and not pd.isna(row[lng_col]):
                try:
                    lng = float(row[lng_col])
                except (ValueError, TypeError):
                    lng = None

            radius = None
            if radius_col and not pd.isna(row[radius_col]):
                try:
                    radius = int(round(float(row[radius_col])))
                except (ValueError, TypeError):
                    radius = None

            signature = generate_query_signature(qp, lat, lng, radius)
            queries.append({
                "row_index": idx + 1,
                "qp": qp,
                "lat": lat,
                "lng": lng,
                "radius": radius,
                "query_signature": signature,
            })
        except Exception as e:
            logging.warning("Skipping malformed row %d: %s (Error: %s)", idx + 1, row.to_dict(), e)

    return queries


def chunk_list(items: List[Any], chunk_size: int):
    """Yield successive chunks of size chunk_size from items."""
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


async def process_batch(
    batch_num: int,
    batch_queries: List[Dict[str, Any]],
    client: FamilySearchClient,
    db: aiosqlite.Connection,
    reports_dir: Path,
    force_playwright: bool = False,
) -> Dict[str, Any]:
    """
    Process a single batch of queries:
    1. Check SQLite cache.
    2. Fetch uncached queries via FamilySearchClient (Direct API or Playwright).
    3. Save newly fetched queries & locations to SQLite.
    4. Export batch report to timestamped Excel file.
    """
    logger = logging.getLogger("process_batch")
    logger.info("=" * 60)
    logger.info("Starting Batch %d (%d queries)", batch_num, len(batch_queries))
    logger.info("=" * 60)

    batch_records: List[Dict[str, Any]] = []
    uncached_queries: List[Dict[str, Any]] = []
    cached_count = 0

    # Step 1: Cache-First Database Lookup
    for q in batch_queries:
        cached_result = await get_cached_query(db, q["query_signature"])
        if cached_result:
            logger.info("CACHE HIT: '%s' [%d locations]", q["qp"], len(cached_result["locations"]))
            batch_records.append({
                "query": cached_result["query"],
                "locations": cached_result["locations"],
                "is_cached": True,
            })
            cached_count += 1
        else:
            uncached_queries.append(q)

    # Step 2: Fetch Uncached Queries Concurrently
    fetched_count = 0
    new_locations_count = 0

    if uncached_queries:
        logger.info(
            "Batch %d: %d queries cached, %d queries to fetch externally",
            batch_num, cached_count, len(uncached_queries)
        )

        async def fetch_and_save(q_item: Dict[str, Any]) -> Dict[str, Any]:
            qp = q_item["qp"]
            lat = q_item["lat"]
            lng = q_item["lng"]
            radius = q_item["radius"]
            sig = q_item["query_signature"]

            if lat is not None and lng is not None:
                logger.info("FETCHING: '%s' (lat=%.6f, lng=%.6f, r=%s)", qp, lat, lng, radius or 50)
            else:
                logger.info("FETCHING: '%s' (place query only)", qp)

            try:
                locations = await client.fetch_locations(
                    qp=qp,
                    lat=lat,
                    lng=lng,
                    radius=radius,
                    force_playwright=force_playwright,
                )
            except Exception as exc:
                logger.error("Error fetching '%s': %s", qp, exc, exc_info=True)
                locations = []

            # Transactionally save query and locations into SQLite
            query_record = {
                "query_signature": sig,
                "qp": qp,
                "lat": lat,
                "lng": lng,
                "radius": radius,
            }
            await save_query_and_results(db, query_record, locations)

            return {
                "query": query_record,
                "locations": locations,
                "is_cached": False,
            }

        fetch_results = await asyncio.gather(
            *[fetch_and_save(q) for q in uncached_queries],
            return_exceptions=False,
        )

        for res in fetch_results:
            batch_records.append(res)
            fetched_count += 1
            new_locations_count += len(res["locations"])

    # Step 3: Generate Batch Excel Report
    report_file = export_batch_report(
        batch_number=batch_num,
        batch_records=batch_records,
        reports_dir=reports_dir,
    )

    total_locations = sum(len(r["locations"]) for r in batch_records)
    logger.info(
        "Completed Batch %d Summary: %d queries total (%d cached, %d fetched) -> %d locations exported to %s",
        batch_num, len(batch_queries), cached_count, fetched_count, total_locations, report_file.name
    )

    return {
        "batch_num": batch_num,
        "total_queries": len(batch_queries),
        "cached_queries": cached_count,
        "fetched_queries": fetched_count,
        "total_locations": total_locations,
        "report_file": str(report_file),
    }


async def run_pipeline(
    input_file: Path,
    batch_size: int = BATCH_SIZE,
    max_concurrency: int = MAX_CONCURRENCY,
    db_path: Path = DB_PATH,
    reports_dir: Path = REPORTS_DIR,
    force_playwright: bool = False,
) -> None:
    """
    Execute the entire FamilySearch scraping and caching pipeline.
    """
    logger = logging.getLogger("run_pipeline")
    start_time = datetime.now()
    logger.info("Initializing FamilySearch scraping pipeline...")
    logger.info("Database: %s | Reports Directory: %s", db_path, reports_dir)
    logger.info("Batch Size: %d | Max Concurrency: %d | Playwright Mode: %s",
                batch_size, max_concurrency, force_playwright)

    # 1. Initialize SQLite database
    await init_db(db_path)

    # 2. Load input queries
    queries = load_input_queries(input_file)
    logger.info("Loaded %d valid input queries from %s", len(queries), input_file)
    if not queries:
        logger.warning("No queries to process. Exiting.")
        return

    # 3. Initialize scraper client
    client = FamilySearchClient(max_concurrency=max_concurrency)

    batch_summaries: List[Dict[str, Any]] = []

    try:
        # Open persistent connection for batch loop
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            
            batches = list(chunk_list(queries, batch_size))
            logger.info("Divided %d queries into %d batches of size %d", len(queries), len(batches), batch_size)

            for batch_idx, batch_queries in enumerate(batches, start=1):
                summary = await process_batch(
                    batch_num=batch_idx,
                    batch_queries=batch_queries,
                    client=client,
                    db=db,
                    reports_dir=reports_dir,
                    force_playwright=force_playwright,
                )
                batch_summaries.append(summary)

    finally:
        await client.close()

    elapsed = (datetime.now() - start_time).total_seconds()
    
    # Final Pipeline Summary
    logger.info("\n" + "=" * 65)
    logger.info("PIPELINE EXECUTION COMPLETED IN %.2f SECONDS", elapsed)
    logger.info("=" * 65)
    total_q = sum(s["total_queries"] for s in batch_summaries)
    total_cached = sum(s["cached_queries"] for s in batch_summaries)
    total_fetched = sum(s["fetched_queries"] for s in batch_summaries)
    total_locs = sum(s["total_locations"] for s in batch_summaries)

    logger.info("Total Queries Processed: %d", total_q)
    logger.info("Total Cached (Skipped Request): %d", total_cached)
    logger.info("Total Fetched from Network: %d", total_fetched)
    logger.info("Total Location Rows Exported: %d", total_locs)
    logger.info("Reports Generated (%d):", len(batch_summaries))
    for s in batch_summaries:
        logger.info("  - Batch %d: %s (%d locations)", s["batch_num"], s["report_file"], s["total_locations"])
    logger.info("=" * 65)


def main():
    """Command-line argument parser and runner."""
    parser = argparse.ArgumentParser(
        description="Production FamilySearch Locations Scraper, SQLite Cache & Excel Reporter."
    )
    default_input = Path("input_queries.xlsx") if Path("input_queries.xlsx").exists() else Path("input_queries.csv")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=default_input,
        help=f"Path to input queries Excel (.xlsx, .xls) or CSV file (default: {default_input})",
    )
    parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=BATCH_SIZE,
        help=f"Number of queries per batch / Excel export (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=MAX_CONCURRENCY,
        help=f"Maximum concurrent network requests (default: {MAX_CONCURRENCY})",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DB_PATH,
        help=f"SQLite database file path (default: {DB_PATH})",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help=f"Output directory for generated Excel reports (default: {REPORTS_DIR})",
    )
    parser.add_argument(
        "--force-playwright",
        action="store_true",
        help="Force using Playwright headless browser network interception instead of direct REST API",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable detailed debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        asyncio.run(
            run_pipeline(
                input_file=args.input,
                batch_size=args.batch_size,
                max_concurrency=args.concurrency,
                db_path=args.db_path,
                reports_dir=args.reports_dir,
                force_playwright=args.force_playwright,
            )
        )
    except KeyboardInterrupt:
        logging.info("Pipeline cancelled by user.")
        sys.exit(130)
    except Exception as e:
        logging.critical("Fatal error executing pipeline: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
