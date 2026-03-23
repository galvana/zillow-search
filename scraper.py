"""
Zillow scraper module.

Uses Playwright with stealth patches to browse Zillow and extract listing data.
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

# JavaScript to patch common bot-detection signals
STEALTH_SCRIPTS = """
// Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Add chrome runtime object
window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };

// Fix permissions query
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);

// Add plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        { name: 'Native Client', filename: 'internal-nacl-plugin' },
    ],
});

// Fix languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// Fix platform
Object.defineProperty(navigator, 'platform', {
    get: () => 'MacIntel',
});

// Prevent iframe detection of automation
for (let i = 0; i < 10; i++) {
    const frame = document.createElement('iframe');
    frame.style.display = 'none';
    document.body.appendChild(frame);
    const contentWindow = frame.contentWindow;
    if (contentWindow) {
        Object.defineProperty(contentWindow.navigator, 'webdriver', { get: () => undefined });
    }
    document.body.removeChild(frame);
}
"""


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


def _parse_price(price_str: str) -> int:
    """Parse a price string like '$450,000' into an integer."""
    digits = re.sub(r"[^\d]", "", price_str)
    return int(digits) if digits else 0


def _extract_results_from_scripts(page) -> list[dict]:
    """Extract search result data from embedded scripts in the page."""
    # Try __NEXT_DATA__ script tag first (Next.js SSR data)
    try:
        next_data = page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__');
            if (el) return JSON.parse(el.textContent);
            return null;
        }""")
        if next_data:
            props = next_data.get("props", {}).get("pageProps", {})
            cat1 = props.get("searchPageState", {}).get("cat1", {})
            results = cat1.get("searchResults", {}).get("listResults", [])
            if results:
                logger.info("Extracted %d results from __NEXT_DATA__", len(results))
                return results
    except Exception:
        logger.debug("__NEXT_DATA__ extraction failed", exc_info=True)

    # Fallback: search inline scripts for search result data
    try:
        results = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const text = s.textContent || '';
                if (text.includes('"listResults"') && text.includes('"zpid"')) {
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


def _extract_results_from_dom(page) -> list[dict]:
    """Extract listing data directly from DOM property cards as last resort."""
    try:
        cards = page.evaluate("""() => {
            const results = [];
            // Try multiple selectors for property cards
            const selectors = [
                'article[data-test="property-card"]',
                '[data-test="property-card"]',
                'li article[id]',
                '.property-card-data',
                '[class*="ListItem"]',
                '[class*="StyledPropertyCard"]',
            ];

            let elements = [];
            for (const sel of selectors) {
                elements = document.querySelectorAll(sel);
                if (elements.length > 0) break;
            }

            for (const el of elements) {
                try {
                    // Extract zpid from element id or data attributes
                    let zpid = el.getAttribute('data-zpid') || el.id || '';
                    zpid = zpid.replace(/[^0-9]/g, '');

                    // Extract address
                    const addrEl = el.querySelector('[data-test="property-card-addr"], address, [class*="address"]');
                    const address = addrEl ? addrEl.textContent.trim() : '';

                    // Extract price
                    const priceEl = el.querySelector('[data-test="property-card-price"], [class*="price"]');
                    const priceText = priceEl ? priceEl.textContent.trim() : '';

                    // Extract beds/baths/sqft
                    const detailEl = el.querySelector('[data-test="property-card-details"], [class*="details"]');
                    const detailText = detailEl ? detailEl.textContent : '';

                    // Extract link
                    const linkEl = el.querySelector('a[href*="/homedetails/"]') || el.querySelector('a[href]');
                    const detailUrl = linkEl ? linkEl.getAttribute('href') : '';

                    // Extract image
                    const imgEl = el.querySelector('img[src]');
                    const imgSrc = imgEl ? imgEl.getAttribute('src') : '';

                    if (zpid || address) {
                        results.push({
                            zpid: zpid,
                            address: address,
                            price: priceText,
                            detailText: detailText,
                            detailUrl: detailUrl,
                            imgSrc: imgSrc,
                        });
                    }
                } catch(e) {}
            }
            return results;
        }""")

        if not cards:
            return []

        logger.info("Extracted %d results from DOM property cards", len(cards))

        # Convert DOM data into the standard result format
        parsed = []
        for card in cards:
            beds = None
            baths = None
            sqft = None
            detail_text = card.get("detailText", "")
            if detail_text:
                bed_match = re.search(r"(\d+)\s*b(?:d|ed)", detail_text, re.I)
                bath_match = re.search(r"(\d+(?:\.\d+)?)\s*ba", detail_text, re.I)
                sqft_match = re.search(r"([\d,]+)\s*sq\s*ft", detail_text, re.I)
                if bed_match:
                    beds = int(bed_match.group(1))
                if bath_match:
                    baths = float(bath_match.group(1))
                if sqft_match:
                    sqft = int(sqft_match.group(1).replace(",", ""))

            price_text = card.get("price", "")
            price = _parse_price(price_text)

            photos = []
            if card.get("imgSrc"):
                photos.append(card["imgSrc"])

            parsed.append({
                "zpid": card.get("zpid", ""),
                "address": card.get("address", "Unknown"),
                "unformattedPrice": price,
                "beds": beds,
                "baths": baths,
                "area": sqft,
                "detailUrl": card.get("detailUrl", ""),
                "carouselPhotos": [{"url": u} for u in photos],
                "homeType": "Unknown",
            })

        return parsed

    except Exception:
        logger.debug("DOM extraction failed", exc_info=True)
        return []


def _extract_api_results(response_data: list[dict]) -> list[dict]:
    """Extract listing results from captured API responses."""
    for data in response_data:
        cat1 = data.get("cat1", {})
        results = cat1.get("searchResults", {}).get("listResults", [])
        if results:
            return results
    return []


def _log_page_diagnostics(page) -> None:
    """Log diagnostic info about the current page state."""
    try:
        title = page.title()
        url = page.url
        logger.info("Page diagnostics - URL: %s, Title: %s", url, title)

        # Check for common block/CAPTCHA indicators
        indicators = page.evaluate("""() => {
            const body = document.body ? document.body.innerText.substring(0, 500) : '';
            const hasCaptcha = !!(
                document.querySelector('#captcha-box') ||
                document.querySelector('[class*="captcha"]') ||
                document.querySelector('iframe[src*="captcha"]') ||
                document.querySelector('#px-captcha') ||
                body.includes('Press & Hold') ||
                body.includes('verify you are a human') ||
                body.includes('Access Denied')
            );
            const hasResults = !!(
                document.querySelector('article[data-test="property-card"]') ||
                document.querySelector('[id="grid-search-results"]') ||
                document.querySelector('[class*="ListItem"]')
            );
            const scriptCount = document.querySelectorAll('script').length;
            return {
                hasCaptcha,
                hasResults,
                scriptCount,
                bodyPreview: body.substring(0, 300),
            };
        }""")
        logger.info("Page state: captcha=%s, results=%s, scripts=%d",
                     indicators.get("hasCaptcha"), indicators.get("hasResults"),
                     indicators.get("scriptCount", 0))
        if indicators.get("hasCaptcha"):
            logger.error("CAPTCHA/bot detection triggered! Body preview: %s",
                         indicators.get("bodyPreview", ""))
        elif not indicators.get("hasResults"):
            logger.warning("No result elements found. Body preview: %s",
                           indicators.get("bodyPreview", ""))
    except Exception:
        logger.debug("Failed to get page diagnostics", exc_info=True)


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

    Uses Playwright with stealth patches to render the search page in a real
    browser, bypassing API-level anti-bot protections.

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
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
            },
        )

        # Inject stealth scripts before any page loads
        context.add_init_script(STEALTH_SCRIPTS)

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
            logger.info("Loading search page: %s", full_url)
            page.goto(full_url, wait_until="domcontentloaded", timeout=30000)

            # Wait for results to render
            try:
                page.wait_for_selector(
                    'article[data-test="property-card"], [id="grid-search-results"], '
                    '[class*="ListItem"], [class*="StyledPropertyCard"]',
                    timeout=15000,
                )
                logger.info("Search results rendered successfully")
            except PlaywrightTimeout:
                logger.warning("Timed out waiting for search results to render")

            # Give a moment for any remaining API calls
            time.sleep(2)

            # Log diagnostics to understand page state
            _log_page_diagnostics(page)

            # Strategy 1: Try captured API responses (most reliable data)
            result_list = _extract_api_results(api_responses)
            if result_list:
                logger.info("Got %d results from intercepted API response", len(result_list))

            # Strategy 2: Extract from embedded scripts
            if not result_list:
                result_list = _extract_results_from_scripts(page)

            # Strategy 3: Parse directly from DOM as last resort
            if not result_list:
                logger.info("Falling back to DOM-based extraction")
                result_list = _extract_results_from_dom(page)

            if not result_list:
                logger.error(
                    "All extraction strategies failed. "
                    "Zillow may be blocking this request."
                )

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
