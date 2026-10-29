"""
Database module for caching FamilySearch queries and location results using SQLite.
Supports asynchronous operations via aiosqlite with foreign key integrity.
"""
from pathlib import Path
from typing import Optional, List, Dict, Any
import aiosqlite
import json
import logging

from config import DB_PATH

logger = logging.getLogger(__name__)


def generate_query_signature(qp: str, lat: float | str, lng: float | str, radius: int | float | str) -> str:
    """
    Generate a normalized, deterministic query signature for caching.
    Format: normalized_qp|lat_6f|lng_6f|radius_int
    """
    normalized_qp = " ".join((str(qp) or "").strip().lower().split())
    lat_val = f"{float(lat):.6f}"
    lng_val = f"{float(lng):.6f}"
    radius_val = str(int(round(float(radius))))
    return f"{normalized_qp}|{lat_val}|{lng_val}|{radius_val}"


async def init_db(db_path: Path = DB_PATH) -> None:
    """
    Initialize SQLite database schema and indices.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        
        # 1. search_queries table
        await db.execute(
            """
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
            """
        )
        
        # 2. location_results table
        await db.execute(
            """
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
            """
        )
        
        # Indexes for rapid lookup
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_queries_sig ON search_queries(query_signature);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_location_results_qid ON location_results(query_id);"
        )
        await db.commit()
    logger.info("Database initialized successfully at %s", db_path)


async def get_cached_query(db: aiosqlite.Connection, query_signature: str) -> Optional[Dict[str, Any]]:
    """
    Check if a query exists in the database.
    If it exists, return the query metadata and associated location results.
    Returns None if not found in cache.
    """
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT id, query_signature, qp, lat, lng, radius, result_count, created_at
        FROM search_queries
        WHERE query_signature = ?
        """,
        (query_signature,),
    ) as cursor:
        query_row = await cursor.fetchone()
        
    if not query_row:
        return None
        
    query_id = query_row["id"]
    
    # Retrieve all associated locations
    locations = []
    async with db.execute(
        """
        SELECT 
            id, query_id, location_entity_id, location_name, location_type,
            street_address, city, state_region, postal_code, country,
            phone, website_url, res_lat, res_lng, distance_miles,
            hours_or_notes, raw_json
        FROM location_results
        WHERE query_id = ?
        ORDER BY distance_miles ASC, id ASC
        """,
        (query_id,),
    ) as cursor:
        async for row in cursor:
            locations.append(dict(row))
            
    return {
        "query": dict(query_row),
        "locations": locations,
        "is_cached": True,
    }


async def save_query_and_results(
    db: aiosqlite.Connection,
    query_data: Dict[str, Any],
    locations: List[Dict[str, Any]],
) -> int:
    """
    Atomically insert search query and all returned location results into SQLite.
    Returns the generated query_id.
    """
    await db.execute("PRAGMA foreign_keys = ON;")
    
    query_signature = query_data["query_signature"]
    qp = query_data.get("qp")
    lat = query_data.get("lat")
    lng = query_data.get("lng")
    radius = query_data.get("radius")
    result_count = len(locations)
    
    # Transactional execution
    cursor = await db.execute(
        """
        INSERT INTO search_queries (query_signature, qp, lat, lng, radius, result_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(query_signature) DO UPDATE SET
            result_count = excluded.result_count
        """,
        (query_signature, qp, lat, lng, radius, result_count),
    )
    query_id = cursor.lastrowid
    
    # In case of conflict update where lastrowid might be 0, look up existing id
    if not query_id:
        async with db.execute(
            "SELECT id FROM search_queries WHERE query_signature = ?",
            (query_signature,),
        ) as id_cursor:
            row = await id_cursor.fetchone()
            query_id = row[0]
            
        # Clear out prior locations for this query to avoid duplicates
        await db.execute("DELETE FROM location_results WHERE query_id = ?", (query_id,))
    
    if locations:
        location_tuples = []
        for loc in locations:
            raw_json_str = (
                json.dumps(loc["raw_json"])
                if isinstance(loc.get("raw_json"), (dict, list))
                else str(loc.get("raw_json") or "")
            )
            location_tuples.append((
                query_id,
                loc.get("location_entity_id"),
                loc.get("location_name"),
                loc.get("location_type"),
                loc.get("street_address"),
                loc.get("city"),
                loc.get("state_region"),
                loc.get("postal_code"),
                loc.get("country"),
                loc.get("phone"),
                loc.get("website_url"),
                loc.get("res_lat"),
                loc.get("res_lng"),
                loc.get("distance_miles"),
                loc.get("hours_or_notes"),
                raw_json_str,
            ))
            
        await db.executemany(
            """
            INSERT INTO location_results (
                query_id, location_entity_id, location_name, location_type,
                street_address, city, state_region, postal_code, country,
                phone, website_url, res_lat, res_lng, distance_miles,
                hours_or_notes, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            location_tuples,
        )
        
    await db.commit()
    logger.debug("Saved query '%s' with %d locations (query_id=%d)", query_signature, len(locations), query_id)
    return query_id
