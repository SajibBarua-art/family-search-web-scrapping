"""
Modular scraper and API client for FamilySearch Locations.
Supports:
1. High-throughput Direct HTTP API mode via httpx with concurrency limiting (asyncio.Semaphore)
   and exponential backoff retry logic (tenacity).
2. Auto-Discovery & Playwright fallback mode with network XHR/Fetch interception.
"""
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote_plus, unquote
import asyncio
import json
import logging
import re
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

from config import (
    API_BASE_URL,
    SEARCH_PAGE_URL,
    DEFAULT_API_KEY,
    EXPERIENCE_KEY,
    VERTICAL_KEY,
    API_VERSION,
    EXPERIENCE_VERSION,
    DEFAULT_LOCALE,
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    MAX_CONCURRENCY,
    MAX_RETRIES,
    BACKOFF_MIN,
    BACKOFF_MAX,
)

logger = logging.getLogger(__name__)


def is_retryable_http_error(exception: BaseException) -> bool:
    """
    Retry on network errors, timeouts, HTTP 429 (Rate Limit), and 5xx Server Errors.
    """
    if isinstance(exception, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        code = exception.response.status_code
        return code == 429 or 500 <= code < 600
    return False


def format_hours_summary(hours_data: Any, notes: Optional[str] = None) -> str:
    """
    Format hours dictionary and notes into a clean, human-readable string.
    """
    parts = []
    if notes:
        parts.append(f"Notes: {notes.strip()}")
        
    if isinstance(hours_data, dict):
        days_order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        day_strs = []
        for day in days_order:
            day_info = hours_data.get(day)
            if not day_info:
                continue
            if day_info.get("isClosed"):
                day_strs.append(f"{day.capitalize()}: Closed")
            elif day_info.get("openIntervals"):
                intervals = [f"{iv.get('start', '')}-{iv.get('end', '')}" for iv in day_info["openIntervals"]]
                day_strs.append(f"{day.capitalize()}: {', '.join(intervals)}")
        if day_strs:
            parts.append("; ".join(day_strs))
            
    return " | ".join(parts) if parts else ""


class FamilySearchClient:
    """
    Client for querying FamilySearch Locations.
    Supports direct REST API queries with concurrency control and Playwright fallback.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_concurrency: int = MAX_CONCURRENCY,
        api_base_url: str = API_BASE_URL,
    ):
        self.api_key = api_key or DEFAULT_API_KEY
        self.api_base_url = api_base_url
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._discovered_credentials: Dict[str, str] = {}

    async def get_http_client(self) -> httpx.AsyncClient:
        """
        Get or initialize the shared persistent httpx.AsyncClient.
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        """
        Close the underlying HTTP client session.
        """
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def _parse_location_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract normalized location fields from a Yext Answers/Search vertical item.
        """
        data = item.get("data", {})
        addr = data.get("address", {}) or {}
        
        # Street address combination (line1 + line2)
        lines = [addr.get("line1", "").strip(), addr.get("line2", "").strip()]
        street_address = ", ".join([line for line in lines if line])
        
        # Coordinates
        coord = (
            data.get("yextDisplayCoordinate")
            or data.get("displayCoordinate")
            or data.get("geocodedCoordinate")
            or {}
        )
        res_lat = coord.get("latitude")
        res_lng = coord.get("longitude")
        
        # Distance calculation
        distance_meters = item.get("distanceFromFilter") or item.get("distance")
        distance_miles = None
        if distance_meters is not None:
            try:
                distance_miles = round(float(distance_meters) / 1609.344, 2)
            except (ValueError, TypeError):
                distance_miles = None
                
        # Website URL
        website_obj = data.get("websiteUrl")
        website_url = None
        if isinstance(website_obj, dict):
            website_url = website_obj.get("url")
        elif isinstance(website_obj, str):
            website_url = website_obj
        if not website_url:
            website_url = data.get("website")
            
        # Hours / Notes
        hours_data = data.get("hours")
        notes = data.get("c_locatorNotes") or data.get("description")
        hours_or_notes = format_hours_summary(hours_data, notes)
        
        # Type
        loc_type = (
            data.get("c_fSDifferentiator")
            or data.get("c_locationType")
            or data.get("locationType")
            or "Location"
        )
        
        return {
            "location_entity_id": str(data.get("id") or ""),
            "location_name": data.get("name") or "FamilySearch Center",
            "location_type": loc_type,
            "street_address": street_address,
            "city": addr.get("city") or "",
            "state_region": addr.get("region") or "",
            "postal_code": addr.get("postalCode") or "",
            "country": addr.get("countryCode") or "",
            "phone": data.get("mainPhone") or "",
            "website_url": website_url or "",
            "res_lat": res_lat,
            "res_lng": res_lng,
            "distance_miles": distance_miles,
            "hours_or_notes": hours_or_notes,
            "raw_json": item,
        }

    async def auto_discover_credentials(self) -> str:
        """
        Scrape search page HTML to auto-discover the active API key.
        """
        client = await self.get_http_client()
        try:
            resp = await client.get(SEARCH_PAGE_URL)
            resp.raise_for_status()
            html = resp.text
            
            # Match URI-encoded pageProps
            match = re.search(r'decodeURIComponent\(\"([^\"]+)\"\)', html)
            if match:
                decoded = unquote(match.group(1))
                page_data = json.loads(decoded)
                site_data = page_data.get("document", {}).get("_site", {})
                api_key = site_data.get("c_searchExperienceAPIKey")
                if api_key:
                    logger.info("Auto-discovered active Search Experience API key: %s", api_key)
                    self.api_key = api_key
                    return api_key
        except Exception as e:
            logger.warning("Auto-discovery via HTML regex failed: %s", e)
            
        return self.api_key

    async def _execute_direct_request(
        self,
        lat: float,
        lng: float,
        radius_miles: int,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        Execute an HTTP request to the Yext vertical query API with tenacity retries.
        """
        client = await self.get_http_client()
        radius_meters = int(round(radius_miles * 1609.344))
        
        # Spatial $near filter matching frontend locator format
        filters_dict = {
            "builtin.location": {
                "$near": {
                    "lat": float(lat),
                    "lng": float(lng),
                    "radius": radius_meters,
                }
            }
        }
        
        params = {
            "experienceKey": EXPERIENCE_KEY,
            "api_key": self.api_key,
            "v": API_VERSION,
            "version": EXPERIENCE_VERSION,
            "locale": DEFAULT_LOCALE,
            "verticalKey": VERTICAL_KEY,
            "filters": json.dumps(filters_dict),
            "limit": str(limit),
            "retrieveFacets": "true",
            "skipSpellCheck": "true",
        }
        
        @retry(
            reraise=True,
            stop=stop_after_attempt(MAX_RETRIES),
            wait=wait_exponential(multiplier=BACKOFF_MIN, max=BACKOFF_MAX),
            retry=retry_if_exception(is_retryable_http_error),
            before_sleep=before_sleep_log(logger, logging.WARNING),
        )
        async def _req():
            async with self.semaphore:
                response = await client.get(self.api_base_url, params=params)
                response.raise_for_status()
                return response.json()
                
        return await _req()

    async def query_direct_api(
        self,
        qp: str,
        lat: float,
        lng: float,
        radius: int,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Direct HTTP API mode (Primary).
        Queries Yext locator endpoint and parses returned locations.
        """
        try:
            data = await self._execute_direct_request(lat, lng, radius, limit=limit)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning("Received HTTP %d; refreshing API key...", e.response.status_code)
                await self.auto_discover_credentials()
                data = await self._execute_direct_request(lat, lng, radius, limit=limit)
            else:
                raise
                
        raw_results = data.get("response", {}).get("results", [])
        locations = [self._parse_location_item(r) for r in raw_results]
        logger.debug(
            "Direct API for '%s' (lat=%f, lng=%f, r=%d) returned %d locations",
            qp, lat, lng, radius, len(locations)
        )
        return locations

    async def query_playwright_interception(
        self,
        qp: str,
        lat: float,
        lng: float,
        radius: int,
        timeout_ms: int = 25000,
    ) -> List[Dict[str, Any]]:
        """
        Auto-Discovery / Playwright Fallback Mode.
        Opens the FamilySearch search page in headless Playwright, intercepts
        the XHR/Fetch JSON response containing location entities, and extracts results.
        Uses page.expect_response for fast, deterministic interception.
        """
        from playwright.async_api import async_playwright
        
        target_url = (
            f"{SEARCH_PAGE_URL}?"
            f"qp={quote_plus(qp)}&"
            f"lat={lat}&"
            f"lng={lng}&"
            f"r={radius}"
        )
        logger.info("Opening Playwright session for: %s", target_url)
        
        captured_data: Optional[Dict[str, Any]] = None
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"]
            )
            context = await browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
            page = await context.new_page()
            
            try:
                async with page.expect_response(
                    lambda r: "search/vertical/query" in r.url and r.status == 200,
                    timeout=timeout_ms,
                ) as resp_info:
                    await page.goto(target_url, wait_until="commit", timeout=timeout_ms)
                    
                resp = await resp_info.value
                url = resp.url
                match_key = re.search(r'api_key=([^&]+)', url)
                if match_key:
                    self.api_key = match_key.group(1)
                    
                captured_data = await resp.json()
            except Exception as e:
                logger.warning("Playwright response interception error for '%s': %s", qp, e)
            finally:
                await browser.close()
            
        if captured_data:
            raw_results = captured_data.get("response", {}).get("results", [])
            locations = [self._parse_location_item(r) for r in raw_results]
            logger.info("Playwright interception returned %d locations", len(locations))
            return locations
            
        logger.warning("Playwright interception did not capture search response for '%s'", qp)
        return []

    async def fetch_locations(
        self,
        qp: str,
        lat: float,
        lng: float,
        radius: int,
        force_playwright: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Unified fetching method:
        - If force_playwright is True: uses Playwright network interception.
        - Otherwise: uses Direct HTTP API mode, falling back to Playwright on failure.
        """
        if force_playwright:
            return await self.query_playwright_interception(qp, lat, lng, radius)
            
        try:
            return await self.query_direct_api(qp, lat, lng, radius)
        except Exception as e:
            logger.warning(
                "Direct API query failed for '%s' (%s). Falling back to Playwright interception...",
                qp, e
            )
            return await self.query_playwright_interception(qp, lat, lng, radius)
