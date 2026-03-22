"""
Zillow scraper module.

Uses Playwright to browse Zillow and extract listing data, avoiding API-level blocking.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

ZILLOW_LISTING_URL = "https://www.zillow.com/homedetails/{zpid}_zpid/"

# Mapping from config home types to Zillow's internal type codes
HOME_TYPE_MAP = {
    "SingleFamily": "Houses",
    "Condo": "Condos",
    "Townhouse": "Townhouses",
    "MultiFamily": "Multi-family",
    "Manufactured": "Manufactured",
    "Land": "LotsLand",
    "Apartment": "Apartments",
}


@dataclass
class Listing:
    """Represents a single Zillow listing."""

    zpid: str
    address: str
    price: int
    beds: int | None
    baths: float | None
    sqft: int | None
    lot_sqft: int | None
    home_type: str
    year_built: int | None
    days_on_zillow: int | None
    url: str
    photo_urls: list[str] = field(default_factory=list)
    description: str = ""
    latitude: float | None = None
    longitude: float | None = None
    zestimate: int | None = None
    raw_data: dict = field(default_factory=dict, repr=False)


def _build_search_url(location: str) -> str:
    """Build a Zillow search URL for the given location."""
    slug = location.lower().strip()
    slug = re.sub(r"[,\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"https://www.zillow.com/{slug}/"


def _build_filter_query_param(config: dict) -> str:
    """Build the searchQueryState filter as a URL query parameter."""
    search = config["search"]
    filter_state = {}

    # Price
    if search.get("min_price") or search.get("max_price"):
        price_filter = {}
        if search.get("min_price"):
            price_filter["min"] = search["min_price"]
        if search.get("max_price"):
            price_filter["max"] = search["max_price"]
        filter_state["price"] = price_filter

    # Beds
    if search.get("min_beds") or search.get("max_beds"):
        beds_filter = {}
        if search.get("min_beds"):
            beds_filter["min"] = search["min_beds"]
        if search.get("max_beds"):
            beds_filter["max"] = search["max_beds"]
        filter_state["beds"] = beds_filter

    # Baths
    if search.get("min_baths") or search.get("max_baths"):
        baths_filter = {}
        if search.get("min_baths"):
            baths_filter["min"] = search["min_baths"]
        if search.get("max_baths"):
            baths_filter["max"] = search["max_baths"]
        filter_state["baths"] = baths_filter

    # Sqft
    if search.get("min_sqft") or search.get("max_sqft"):
        sqft_filter = {}
        if search.get("min_sqft"):
            sqft_filter["min"] = search["min_sqft"]
        if search.get("max_sqft"):
            sqft_filter["max"] = search["max_sqft"]
        filter_state["sqft"] = sqft_filter

    # Lot size
    if search.get("min_lot_sqft") or search.get("max_lot_sqft"):
        lot_filter = {}
        if search.get("min_lot_sqft"):
            lot_filter["min"] = search["min_lot_sqft"]
        if search.get("max_lot_sqft"):
            lot_filter["max"] = search["max_lot_sqft"]
        filter_state["lotSize"] = lot_filter

    # Year built
    if search.get("min_year_built") or search.get("max_year_built"):
        year_filter = {}
        if search.get("min_year_built"):
            year_filter["min"] = search["min_year_built"]
        if search.get("max_year_built"):
            year_filter["max"] = search["max_year_built"]
        filter_state["built"] = year_filter

    # Days on Zillow
    if search.get("days_on_zillow"):
        filter_state["doz"] = {"value": str(search["days_on_zillow"])}

    # Home types - disable types NOT in the list
    home_types = search.get("home_types", [])
    if home_types:
        mapped = {HOME_TYPE_MAP.get(t, t) for t in home_types}
        for zillow_type in HOME_TYPE_MAP.values():
            if zillow_type not in mapped:
                filter_state[zillow_type.lower().replace("-", "")] = {"value": False}

    # Features
    if search.get("has_garage") is True:
        filter_state["hasGarage"] = {"value": True}
    if search.get("has_pool") is True:
        filter_state["hasPool"] = {"value": True}
    if search.get("has_ac") is True:
        filter_state["hasAC"] = {"value": True}

    # Listing status
    status = search.get("status", "ForSale")
    if status == "ForSale":
        filter_state["isForSaleByAgent"] = {"value": True}
        filter_state["isForSaleByOwner"] = {"value": True}
        filter_state["isNewConstruction"] = {"value": False}
        filter_state["isForSaleForeclosure"] = {"value": False}
        filter_state["isComingSoon"] = {"value": False}
        filter_state["isAuction"] = {"value": False}
    elif status == "RecentlySold":
        filter_state["isRecentlySold"] = {"value": True}
    elif status == "ForRent":
        filter_state["isForRent"] = {"value": True}

    query_state = {
        "usersSearchTerm": search.get("location", ""),
        "filterState": filter_state,
        "isListVisible": True,
        "mapZoom": 10,
    }

    return json.dumps(query_state, separators=(",", ":"))


def _parse_listing(result: dict) -> Listing | None:
    """Parse a single search result into a Listing object."""
    try:
        zpid = str(result.get("zpid", ""))
        if not zpid:
            return None

        photos = []
        for photo in result.get("carouselPhotos", []):
            url = photo.get("url", "")
            if url:
                photos.append(url)

        return Listing(
            zpid=zpid,
            address=result.get("address", "Unknown"),
            price=int(result.get("unformattedPrice", result.get("price", 0)) or 0),
            beds=result.get("beds"),
            baths=result.get("baths"),
            sqft=result.get("area"),
            lot_sqft=result.get("lotAreaValue"),
            home_type=result.get("homeType", "Unknown"),
            year_built=result.get("yearBuilt"),
            days_on_zillow=result.get("daysOnZillow"),
            url=result.get("detailUrl", ZILLOW_LISTING_URL.format(zpid=zpid)),
            photo_urls=photos,
            description=result.get("description", ""),
            latitude=result.get("latLong", {}).get("latitude"),
            longitude=result.get("latLong", {}).get("longitude"),
            zestimate=result.get("zestimate"),
            raw_data=result,
        )
    except Exception:
        logger.warning("Failed to parse listing: %s", result.get("zpid", "unknown"), exc_info=True)
        return None


def _extract_results_from_page(page) -> list[dict]:
    """Extract search result data embedded in the page."""
    # Try __NEXT_DATA__ script tag first (Next.js SSR data)
    try:
        next_data = page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__');
            if (el) return JSON.parse(el.textContent);
            return null;
        }""")
        if next_data:
            # Navigate the Next.js data structure to find search results
            props = next_data.get("props", {}).get("pageProps", {})
            cat1 = props.get("searchPageState", {}).get("cat1", {})
            results = cat1.get("searchResults", {}).get("listResults", [])
            if results:
                logger.info("Extracted %d results from __NEXT_DATA__", len(results))
                return results
    except Exception:
        logger.debug("__NEXT_DATA__ extraction failed", exc_info=True)

    # Fallback: try to find results in any inline script containing search data
    try:
        results = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const text = s.textContent || '';
                if (text.includes('"listResults"') && text.includes('"zpid"')) {
                    // Find the JSON object containing listResults
                    const match = text.match(/"listResults"\\s*:\\s*(\\[.*?\\])\\s*[,}]/s);
                    if (match) {
                        try { return JSON.parse(match[1]); } catch(e) {}
                    }
                }
            }
            return null;
        }""")
        if results:
            logger.info("Extracted %d results from inline script", len(results))
            return results
    except Exception:
        logger.debug("Inline script extraction failed", exc_info=True)

    return []


def _extract_api_results(response_data: list[dict]) -> list[dict]:
    """Extract listing results from captured API responses."""
    for data in response_data:
        cat1 = data.get("cat1", {})
        results = cat1.get("searchResults", {}).get("listResults", [])
        if results:
            return results
    return []


def _fetch_listing_details(page, listing: Listing) -> None:
    """Navigate to a listing page and extract additional details."""
    url = listing.url
    if not url.startswith("http"):
        url = f"https://www.zillow.com{url}"

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(1)

        details = page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__');
            if (!el) return null;
            const data = JSON.parse(el.textContent);
            const prop = data?.props?.pageProps?.componentProps?.gdpClientCache;
            if (!prop) return null;
            // gdpClientCache is a JSON string keyed by zpid
            const parsed = JSON.parse(prop);
            const key = Object.keys(parsed)[0];
            const property = parsed[key]?.property;
            if (!property) return null;
            return {
                description: property.description || '',
                photos: (property.responsivePhotos || property.photos || []).map(
                    p => p.mixedSources?.jpeg?.[0]?.url || p.url || ''
                ).filter(Boolean),
                yearBuilt: property.yearBuilt,
            };
        }""")

        if details:
            if details.get("description"):
                listing.description = details["description"]
            if details.get("photos"):
                listing.photo_urls = details["photos"]
            if details.get("yearBuilt"):
                listing.year_built = details["yearBuilt"]

    except Exception:
        logger.debug("Failed to fetch details for %s", listing.zpid, exc_info=True)


def search_listings(config: dict, fetch_details: bool = True) -> list[Listing]:
    """
    Search Zillow for listings matching the configured criteria.

    Uses Playwright to render the search page in a real browser,
    bypassing API-level anti-bot protections.

    Args:
        config: The full application config dict (parsed from config.yaml).
        fetch_details: Whether to fetch full listing details (slower but more data).

    Returns:
        List of Listing objects matching the search criteria.
    """
    location = config["search"].get("location", "")
    logger.info("Searching Zillow for listings in: %s", location)

    search_url = _build_search_url(location)
    filter_param = _build_filter_query_param(config)
    full_url = f"{search_url}?searchQueryState={quote_plus(filter_param)}"

    result_list = []
    api_responses: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        # Intercept API responses to capture search results
        def handle_response(response):
            if "async-create-search-page-state" in response.url:
                try:
                    data = response.json()
                    api_responses.append(data)
                    logger.debug("Captured API response from %s", response.url)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            logger.info("Loading search page: %s", search_url)
            page.goto(full_url, wait_until="domcontentloaded", timeout=30000)

            # Wait for results to render
            try:
                page.wait_for_selector(
                    'article[data-test="property-card"], [id="grid-search-results"]',
                    timeout=15000,
                )
            except PlaywrightTimeout:
                logger.warning("Timed out waiting for search results to render")

            # Give a moment for any remaining API calls
            time.sleep(2)

            # Try API responses first (most reliable data format)
            result_list = _extract_api_results(api_responses)

            # Fall back to extracting from page source
            if not result_list:
                result_list = _extract_results_from_page(page)

            logger.info("Found %d raw results from Zillow", len(result_list))

            # Parse results
            listings = []
            for result in result_list:
                listing = _parse_listing(result)
                if listing:
                    listings.append(listing)

            # Optionally fetch detailed info for each listing
            if fetch_details and listings:
                logger.info("Fetching detailed info for %d listings...", len(listings))
                for listing in listings:
                    _fetch_listing_details(page, listing)
                    time.sleep(1)

        except PlaywrightTimeout:
            logger.error("Timed out loading Zillow search page")
            listings = []
        except Exception:
            logger.error("Failed to search Zillow", exc_info=True)
            listings = []
        finally:
            browser.close()

    # Apply keyword filters
    keywords = config["search"].get("keywords", [])
    if keywords:
        filtered = []
        for listing in listings:
            desc_lower = listing.description.lower()
            if any(kw.lower() in desc_lower for kw in keywords):
                filtered.append(listing)
        logger.info("Filtered to %d listings matching keywords", len(filtered))
        listings = filtered

    logger.info("Returning %d listings", len(listings))
    return listings
