import os
import json
import time
import random
import logging
import cloudscraper
import requests
from bs4 import BeautifulSoup
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 1800))  # seconds

SEARCH_URL = (
    "https://www.halooglasi.com/nekretnine/izdavanje-stanova/beograd-novi-beograd"
    "?cena_d_to=750"
    "&cena_d_unit=4"
    "&kvadratura_d_from=45"
    "&kvadratura_d_unit=1"
    "&namestenost_id_l=562"
    "&dodatno_id_ls=12000001"
    "&ostalo_id_ls=12100001%2C12100002"
    "&page={page}"
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

MAX_RETRIES = 3

EXCLUDE_LOCATIONS = ["ledine"]

SEEN_FILE = Path(os.environ.get("SEEN_FILE", "seen_ids.json"))


# ── Persistence ───────────────────────────────────────────────────────
def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen_ids(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)))


# ── Scraper ───────────────────────────────────────────────────────────
def create_session():
    """Create a cloudscraper session that looks like a real browser."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
    )
    ua = random.choice(USER_AGENTS)
    scraper.headers.update({**HEADERS, "User-Agent": ua})

    # Visit homepage first to establish cookies
    log.info("Establishing session via homepage …")
    scraper.get("https://www.halooglasi.com/", timeout=30)
    time.sleep(random.uniform(2, 4))
    return scraper


def fetch_page(scraper, url: str) -> requests.Response:
    """Fetch a page with retry logic for 403 errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = scraper.get(url, timeout=30)
        if resp.status_code != 403:
            resp.raise_for_status()
            return resp
        wait = attempt * 10 + random.uniform(0, 5)
        log.warning("Got 403 on attempt %d, retrying in %.0fs …", attempt, wait)
        time.sleep(wait)
    resp.raise_for_status()  # raise on final failure


def fetch_listings() -> list[dict]:
    """Fetch all listings from all pages matching the search filters."""
    scraper = create_session()
    all_listings = []
    page = 1

    while True:
        url = SEARCH_URL.format(page=page)
        log.info("Fetching page %d …", page)

        resp = fetch_page(scraper, url)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Only look in the visible list container (not the hidden map one)
        list_container = soup.select_one("#ad-list-2")
        if not list_container:
            break

        items = list_container.select("div.product-item.product-list-item")
        if not items:
            break

        for item in items:
            listing = parse_listing(item)
            if listing:
                all_listings.append(listing)

        page += 1
        time.sleep(random.uniform(2, 5))  # pause between pages

    log.info("Found %d listings total across %d page(s).", len(all_listings), page)
    return all_listings


def parse_listing(item) -> dict | None:
    """Extract structured data from a single listing HTML element."""
    listing_id = item.get("data-id", "")

    # Title & URL
    title_el = item.select_one("h3.product-title a")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    url = title_el.get("href", "")
    if url and not url.startswith("http"):
        url = "https://www.halooglasi.com" + url

    # Location parts
    location_parts = [
        li.get_text(strip=True) for li in item.select("ul.subtitle-places li")
    ]
    location = ", ".join(location_parts)

    # Price
    price_el = item.select_one("div.central-feature span[data-value]")
    price = price_el.get_text(strip=True) if price_el else "N/A"

    # Square footage – first feature item
    sqm = "N/A"
    features = item.select("ul.product-features li .value-wrapper")
    if features:
        raw = features[0].get_text(strip=True)
        # Clean up "53 m2Kvadratura" → "53 m²"
        sqm = raw.replace("Kvadratura", "").replace("m2", "m²").strip()

    # Publish date
    date_el = item.select_one("span.publish-date")
    publish_date = date_el.get_text(strip=True) if date_el else "N/A"

    return {
        "id": listing_id,
        "title": title,
        "url": url,
        "location": location,
        "price": price,
        "sqm": sqm,
        "date": publish_date,
    }


# ── Filter ────────────────────────────────────────────────────────────
def should_exclude(listing: dict) -> bool:
    """Return True if the listing should be excluded (e.g. Ledine)."""
    loc_lower = listing["location"].lower()
    title_lower = listing["title"].lower()
    for excl in EXCLUDE_LOCATIONS:
        if excl in loc_lower or excl in title_lower:
            return True
    return False


# ── Telegram ──────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        log.error("Telegram error: %s", resp.text)


def format_message(listing: dict) -> str:
    return (
        f"🏠 <b>Novi stan na Halo Oglasima!</b>\n\n"
        f"<b>{listing['title']}</b>\n"
        f"📍 {listing['location']}\n"
        f"💰 {listing['price']}\n"
        f"📐 {listing['sqm']}\n"
        f"📅 {listing['date']}\n\n"
        f"🔗 <a href=\"{listing['url']}\">Pogledaj oglas</a>"
    )


# ── Main loop ─────────────────────────────────────────────────────────
def check_new_listings() -> None:
    seen = load_seen_ids()
    first_run = len(seen) == 0

    listings = fetch_listings()
    new_listings = [
        l for l in listings if l["id"] not in seen and not should_exclude(l)
    ]

    if first_run:
        kept = [l for l in listings if not should_exclude(l)]
        log.info(
            "First run – saving %d existing listings without notifying.", len(kept)
        )
        save_seen_ids({l["id"] for l in listings})
        send_telegram(
            f"✅ Halo Oglasi monitor pokrenut!\n"
            f"Pratim {len(kept)} oglasa (isključeno {len(listings) - len(kept)} na Ledinama).\n"
            f"Dobićeš obaveštenje čim se pojavi novi stan."
        )
        return

    if new_listings:
        log.info("Found %d new listing(s)!", len(new_listings))
        for listing in new_listings:
            send_telegram(format_message(listing))
            time.sleep(1)  # don't spam Telegram API
    else:
        log.info("No new listings.")

    # Update seen set with all current listing IDs
    seen.update(l["id"] for l in listings)
    save_seen_ids(seen)


def main() -> None:
    run_once = os.environ.get("RUN_ONCE", "0") == "1"

    if run_once:
        log.info("Running single check …")
        check_new_listings()
    else:
        log.info("Starting Halo Oglasi monitor …")
        log.info("Check interval: %d seconds", CHECK_INTERVAL)
        while True:
            try:
                check_new_listings()
            except Exception:
                log.exception("Error during check")
            log.info("Sleeping %d seconds …", CHECK_INTERVAL)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
