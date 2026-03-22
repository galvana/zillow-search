"""
Zillow scraper module.

Uses Zillow's search API to find listings matching configured criteria.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Zillow search API endpoint (public, used by their frontend)
ZILLOW_SEARCH_URL = "https://www.zillow.com/async-create-search-page-state"
ZILLOW_LISTING_URL = "https://www.zillow.com/homedetails/{zpid}_zpid/"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.zillow.com/",
    "Origin": "https://www.zillow.com",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

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


def _build_search_query(config: dict) -> dict:
    """Build the Zillow search query from config."""
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

    return {
        "searchQueryState": {
            "usersSearchTerm": search.get("location", ""),
            "filterState": filter_state,
            "isListVisible": True,
            "mapZoom": 10,
        },
        "wants": {"cat1": ["listResults"]},
        "requestId": 2,
    }


def _build_search_url(location: str) -> str:
    """Build a Zillow search URL for the given location."""
    # Normalize location to URL-friendly format
    slug = location.lower().strip()
    slug = re.sub(r"[,\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"https://www.zillow.com/{slug}/"


def _parse_listing(result: dict) -> Listing | None:
    """Parse a single search result into a Listing object."""
    try:
        zpid = str(result.get("zpid", ""))
        if not zpid:
            return None

        # Extract photos
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


def _fetch_listing_details(zpid: str, session: requests.Session) -> dict:
    """Fetch detailed listing info including description and more photos."""
    url = f"https://www.zillow.com/graphql/"
    query = {
        "operationName": "ForSaleShopperPlatformFullRenderQuery",
        "variables": {"zpid": int(zpid)},
        "query": """query ForSaleShopperPlatformFullRenderQuery($zpid: ID!) {
            property(zpid: $zpid) {
                description
                photoCount
                photos { url }
                yearBuilt
                lotAreaValue
                lotAreaUnit
            }
        }""",
    }

    try:
        resp = session.post(url, json=query, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", {}).get("property", {})
    except Exception:
        logger.debug("Failed to fetch details for zpid=%s", zpid, exc_info=True)

    return {}


def search_listings(config: dict, fetch_details: bool = True) -> list[Listing]:
    """
    Search Zillow for listings matching the configured criteria.

    Args:
        config: The full application config dict (parsed from config.yaml).
        fetch_details: Whether to fetch full listing details (slower but more data).

    Returns:
        List of Listing objects matching the search criteria.
    """
    location = config["search"].get("location", "")
    logger.info("Searching Zillow for listings in: %s", location)

    session = requests.Session()
    session.headers.update(HEADERS)
    search_url = _build_search_url(location)

    # First, load the search page to get cookies (critical for auth)
    try:
        page_headers = {k: v for k, v in HEADERS.items() if k != "Content-Type"}
        page_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        page_headers["sec-fetch-dest"] = "document"
        page_headers["sec-fetch-mode"] = "navigate"
        page_headers["sec-fetch-site"] = "none"
        page_headers["sec-fetch-user"] = "?1"
        resp = session.get(search_url, headers=page_headers, timeout=15)
        resp.raise_for_status()
        logger.info("Loaded search page, got %d cookies", len(session.cookies))
    except requests.RequestException:
        logger.warning("Failed to load search page, continuing without cookies")

    # Build and send the search API request with retry
    query = _build_search_query(config)
    data = None
    max_retries = 3

    for attempt in range(max_retries):
        try:
            resp = session.put(
                ZILLOW_SEARCH_URL,
                json=query,
                headers={**HEADERS, "Referer": search_url},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "Zillow API request failed (attempt %d/%d), retrying in %ds...",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
            else:
                logger.error("Zillow search API request failed after %d attempts", max_retries, exc_info=True)
                return []
        except ValueError:
            logger.error("Failed to parse Zillow API response as JSON")
            return []

    if data is None:
        return []

    # Parse results
    cat1 = data.get("cat1", {})
    search_results = cat1.get("searchResults", {})
    result_list = search_results.get("listResults", [])

    logger.info("Found %d raw results from Zillow", len(result_list))

    listings = []
    for result in result_list:
        listing = _parse_listing(result)
        if listing:
            listings.append(listing)

    # Optionally fetch detailed info for each listing
    if fetch_details and listings:
        logger.info("Fetching detailed info for %d listings...", len(listings))
        for listing in listings:
            details = _fetch_listing_details(listing.zpid, session)
            if details:
                if details.get("description"):
                    listing.description = details["description"]
                if details.get("photos"):
                    photo_urls = [p["url"] for p in details["photos"] if p.get("url")]
                    if photo_urls:
                        listing.photo_urls = photo_urls
                if details.get("yearBuilt"):
                    listing.year_built = details["yearBuilt"]
            # Rate limit to be polite
            time.sleep(0.5)

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
