import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

from housing_policy import (
    canonical_listing_key,
    contains_commercial_use,
    contains_restricted_eligibility,
    extract_amount,
    extract_postcode,
    is_preferred_postcode,
    listing_matches_policy,
    normalize_text,
)

# Configurations
API_URL = "https://udlejning.cej.dk/find-bolig/overblik?collection=residences&monthlyPrice=0-50000&p=sj%C3%A6lland&_data=routes%2Fsearch%2Flayout"
CAPITALBOLIG_API_URL = "https://capitalbolig.dk/wp-json/wp/v2/bolig?per_page=100&_fields=id,link,title"
JULILIVING_PAGE_URL = "https://juliliving.dk/find-lejebolig/"
JULILIVING_AJAX_FALLBACK_URL = "https://juliliving.dk/wp-admin/admin-ajax.php"
CWOBEL_ISLANDS_BRYGGE_URL = "https://www.cwobel-ejendomme.dk/bolig/ledige-lejemaal/storkoebenhavn/islands-brygge/"
PROPSTEP_SEARCH_URL = "https://app.propstep.com/api/search"
SWEET_HOMES_LIST_URL = "https://sweet-homes.dk/lejebolig/"
HEADERS = {
    "x-remix-response": "yes",
    "Accept": "*/*",
    "Accept-Language": "da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
}
# cityapartment.dk's WAF returns HTTP 454 for requests missing Accept-Language
# (verified empirically: identical request with Accept-Language present -> 200,
# without it -> 454 every time). Keep a dedicated headers dict so this stays
# correct even if the shared HEADERS dict changes shape.
CITY_APARTMENT_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": HEADERS["Accept-Language"],
}
SEEN_IDS_FILE = "seen_ids.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "@user")
DISCORD_MENTION_USER_ID = os.environ.get("DISCORD_MENTION_USER_ID")
DISCORD_MENTION_EVERYONE = os.environ.get("DISCORD_MENTION_EVERYONE", "true")


class WatcherError(Exception):
    """Raised when the watcher cannot complete due to break conditions."""


class CEJRateLimitError(WatcherError):
    """Raised when CEJ keeps returning HTTP 429 after retry attempts."""


def post_discord_payload(payload, max_attempts=5):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]},
    )

    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status in [200, 204]:
                    return True
                print(f"Failed to send Discord payload. Status: {response.status}")
                return False
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_attempts:
                retry_after = 2.0
                try:
                    body = e.read().decode("utf-8")
                    details = json.loads(body)
                    retry_after = float(details.get("retry_after", retry_after))
                except Exception:
                    pass

                # Small buffer to avoid immediate repeat rate-limits.
                sleep_for = max(0.5, retry_after) + 0.25
                print(f"Discord rate-limited (attempt {attempt}/{max_attempts}), retrying in {sleep_for:.2f}s.")
                time.sleep(sleep_for)
                continue

            print(f"HTTP Error sending to Discord: {e}")
            return False
        except urllib.error.URLError as e:
            print(f"URL Error sending to Discord: {e}")
            return False
    return False


def normalize_discord_user_id(raw_value):
    if not raw_value:
        return None

    cleaned = raw_value.strip()
    if cleaned.isdigit():
        return cleaned

    match = re.search(r"\d{6,}", cleaned)
    if match:
        return match.group(0)
    return None


def is_truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def build_discord_mention():
    if is_truthy(DISCORD_MENTION_EVERYONE):
        return "@everyone", {"parse": ["everyone"]}

    user_id = normalize_discord_user_id(DISCORD_MENTION_USER_ID)
    if user_id:
        mention = f"<@{user_id}>"
        return mention, {"parse": ["users"], "users": [user_id]}
    return DISCORD_MENTION, None


def read_non_negative_int_env(name, default):
    value = os.environ.get(name)
    if value is None:
        return default

    try:
        parsed = int(value)
        if parsed < 0:
            raise ValueError
        return parsed
    except ValueError:
        print(f"Invalid value for {name}={value!r}. Falling back to {default}.")
        return default


# Fixed interval used only when adaptive polling is disabled (WATCHER_ADAPTIVE_POLLING=false)
# or as a legacy single-shot mode when WATCHER_RUNS is set to a positive number.
SLEEP_SECONDS = read_non_negative_int_env("WATCHER_SLEEP_SECONDS", 60)

# Legacy single-shot mode: if set to a positive number, the watcher runs exactly
# this many polls (using SLEEP_SECONDS between them) and exits, instead of the
# default continuous adaptive loop. 0 (default) means "use the continuous loop".
RUN_COUNT = read_non_negative_int_env("WATCHER_RUNS", 0)

CEJ_MAX_ATTEMPTS = read_non_negative_int_env("CEJ_MAX_ATTEMPTS", 5)
CEJ_RETRY_BASE_SECONDS = read_non_negative_int_env("CEJ_RETRY_BASE_SECONDS", 10)
CEJ_MAX_PRICE = 18000
EXCLUDED_LOCATION_KEYWORDS = ["rodovre", "hvidovre", "ballerup", "valby", "vanlose"]
CEJ_LOCATION_KEYWORDS = [
    "kobenhavn",
    "frederiksberg",
    "amager",
    "bronshoj",
    "norrebro",
    "vesterbro",
    "osterbro",
    "islands brygge",
]

# --- Adaptive polling -------------------------------------------------------
# CEJ's own API exposes `lastPublishedDate` (set on every publish AND every
# status transition, e.g. available -> reserved). A live snapshot of the
# current CEJ feed (fetched 2026-07-08) showed those timestamps concentrated
# almost entirely on weekday business hours in Copenhagen local time, zero on
# Sat/Sun:
#   08:00-13:00 CPH: ~62% of events (morning/midday peak)
#   13:00-17:00 CPH: ~24% of events (afternoon tail)
#   before 08:00 or after 17:00: ~14%, none on weekends
# This matches the two real Discord detections we have on record (Thu 16:40 and
# Fri 15:52 CPH, both in the afternoon-tail window) and the weaker signal from
# `seen_ids.json` commit timestamps (peak ~12:00-15:00 CPH, almost all
# Mon-Fri). CEJ's API is served directly from their own origin (Fly.io) with
# no CDN in front of it -- repeated 7-8s-apart requests returned identical
# `Via: fly.io`/no-Age/no-ETag responses and ~0.8-1.1s origin latency every
# time, so there is no cache TTL to synchronize against (unlike Kereby's
# CloudFront-fronted feed). CEJ has rate-limited this watcher before, so the
# HOT interval below is deliberately conservative rather than as aggressive as
# Kereby's cache-synced polling.
ADAPTIVE_POLLING = os.environ.get("WATCHER_ADAPTIVE_POLLING", "true").strip().lower() != "false"

POLL_INTERVALS = {
    "HOT": read_non_negative_int_env("WATCHER_POLL_HOT_SECONDS", 45),
    "WARM": read_non_negative_int_env("WATCHER_POLL_WARM_SECONDS", 90),
    "COOL": read_non_negative_int_env("WATCHER_POLL_COOL_SECONDS", 240),
    "COLD": read_non_negative_int_env("WATCHER_POLL_COLD_SECONDS", 900),
}
# Never allow a tier to be tuned faster than this, to stay polite to CEJ's origin.
_MIN_POLL_INTERVAL_SECONDS = 10
for _tier_name in POLL_INTERVALS:
    if POLL_INTERVALS[_tier_name] < _MIN_POLL_INTERVAL_SECONDS:
        POLL_INTERVALS[_tier_name] = _MIN_POLL_INTERVAL_SECONDS

# Copenhagen-local hour windows -> tier (start inclusive, end exclusive).
# Any hour not covered here falls through to COLD.
WEEKDAY_TIER_WINDOWS = [
    (8, 13, "HOT"),    # core publish/status-change window (~62% of observed events)
    (7, 8, "WARM"),    # morning ramp-up
    (13, 18, "WARM"),  # afternoon tail (covers both real Discord detections)
    (18, 22, "COOL"),
]
WEEKEND_TIER_WINDOWS = [
    (9, 20, "COOL"),   # rare, but keep a light watch (weak prior from git history)
]

# Sources that are cheap to fetch (single request or a handful) and are the
# ones this cadence redesign targets (CEJ itself, plus the two "sibling"
# sources bundled into this Mixed Watcher, plus the new City Apartment
# watcher). These are fetched on every poll at the tier-driven interval below.
FAST_SOURCE_NAMES = {"CEJ", "Propstep", "Sweet Homes", "City Apartment"}
# Sources that are either slow (Juli Living: ~6-7s per fetch) or amplify into
# many extra HTTP requests per poll (Capital Bolig fetches one detail page per
# matching listing). Polling these as fast as CEJ would multiply load on sites
# nobody has reported timing problems with, so they get their own, much slower,
# independent cadence instead of inheriting CEJ's fast tier.
SLOW_SOURCE_NAMES = {"Capital Bolig", "Juli Living", "C.W. Obel"}
SLOW_SOURCE_INTERVAL_SECONDS = max(60, read_non_negative_int_env("WATCHER_SLOW_SOURCE_INTERVAL_SECONDS", 600))

MAX_RUNTIME_SECONDS = max(60, read_non_negative_int_env("WATCHER_MAX_RUNTIME_SECONDS", 70 * 60))
EXIT_BUFFER_SECONDS = 60


def _last_sunday_0100_utc(year, month):
    """01:00 UTC on the last Sunday of `month` -- the EU DST switch instant."""
    d = datetime(year, month, 31, 1, 0, 0, tzinfo=timezone.utc)
    while d.weekday() != 6:  # 6 == Sunday
        d -= timedelta(days=1)
    return d


def copenhagen_now(utc_now=None):
    """Current Copenhagen wall-clock time as a naive datetime.

    Dependency-free DST via the EU rule (CET = UTC+1, CEST = UTC+2; summer time
    from the last Sunday of March 01:00 UTC to the last Sunday of October
    01:00 UTC). Avoids zoneinfo/tzdata so the watcher stays zero-dependency
    everywhere (including minimal GitHub Actions runners).
    """
    if utc_now is None:
        utc_now = datetime.now(timezone.utc)
    dst_start = _last_sunday_0100_utc(utc_now.year, 3)
    dst_end = _last_sunday_0100_utc(utc_now.year, 10)
    offset = 2 if dst_start <= utc_now < dst_end else 1
    return (utc_now + timedelta(hours=offset)).replace(tzinfo=None)


def classify_period(local_dt):
    """Map a Copenhagen-local datetime to an activity tier name."""
    windows = WEEKEND_TIER_WINDOWS if local_dt.weekday() >= 5 else WEEKDAY_TIER_WINDOWS
    hour = local_dt.hour
    for start, end, tier in windows:
        if start <= hour < end:
            return tier
    return "COLD"


def get_poll_interval_seconds(local_dt=None):
    """Seconds to sleep before the next fast-source poll.

    Falls back to the flat SLEEP_SECONDS when adaptive polling is disabled.
    """
    if not ADAPTIVE_POLLING:
        return SLEEP_SECONDS
    if local_dt is None:
        local_dt = copenhagen_now()
    tier = classify_period(local_dt)
    return POLL_INTERVALS[tier]
STATUS_LABELS = {
    1: "Available",
    2: "Reserved",
    3: "Rented",
    "available": "Available",
    "ledig": "Available",
    "reserved": "Reserved",
    "reserveret": "Reserved",
    "rented": "Rented",
    "udlejet": "Rented",
}
def safe_console_text(value):
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def normalize_search_text(value):
    return normalize_text(value)


def extract_numeric_value(raw_value):
    return extract_amount(raw_value)


def normalize_listing_status(raw_status):
    if raw_status is None:
        return "unknown"

    if isinstance(raw_status, (int, float)):
        raw_status = int(raw_status)

    normalized = normalize_search_text(raw_status)
    if normalized in STATUS_LABELS:
        return STATUS_LABELS[normalized]
    if raw_status in STATUS_LABELS:
        return STATUS_LABELS[raw_status]
    return str(raw_status)


def is_target_postal_code(post_code):
    return is_preferred_postcode(post_code)


def extract_html_text_lines(html):
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)

    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", unescape(line).replace("\xa0", " ")).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def extract_labeled_text_value(lines, label):
    normalized_label = normalize_search_text(label)
    for index, line in enumerate(lines):
        if normalize_search_text(line) != normalized_label:
            continue
        for candidate in lines[index + 1 :]:
            if normalize_search_text(candidate) == normalized_label:
                continue
            return candidate
    return None


def build_embed_title(name, source):
    if not source:
        return name
    return f"[{source}] {name}"


def format_price_for_display(raw_price):
    price_amount = extract_price_amount(raw_price)
    if price_amount is None:
        text = str(raw_price).strip()
        return text or "Unknown"
    return f"{price_amount} kr/month"


def build_listing_fields(listing):
    fields = [
        {"name": "Status", "value": str(listing.get("status", "unknown")), "inline": True},
        {
            "name": "Price",
            "value": format_price_for_display(listing.get("price", {}).get("amount", "Unknown")),
            "inline": True,
        },
    ]

    size_sqm = extract_numeric_value(listing.get("size_sqm"))
    if size_sqm is not None:
        fields.append({"name": "Area", "value": f"{size_sqm} m²", "inline": True})

    rooms = extract_numeric_value(listing.get("rooms"))
    if rooms is not None:
        fields.append({"name": "Rooms", "value": str(rooms), "inline": True})

    fields.extend(
        [
            {
                "name": "Address",
                "value": listing.get("location", {}).get("formatted", "Unknown Address"),
                "inline": True,
            },
            {"name": "Available From", "value": str(listing.get("availableFrom", "Unknown Date")), "inline": True},
        ]
    )

    return fields


def normalize_cej_listing(item):
    listing = dict(item)
    listing["source"] = listing.get("source") or "CEJ"
    listing["status"] = normalize_listing_status(item.get("status"))
    listing["size_sqm"] = extract_numeric_value(item.get("floorSize"))
    listing["rooms"] = extract_numeric_value(item.get("numberOfRooms"))
    return listing

def load_seen_states():
    if os.path.exists(SEEN_IDS_FILE):
        try:
            with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    # Legacy format (list of IDs), assume unknown status to prevent spam migration
                    return {item: "unknown" for item in data}
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"Error loading seen states: {e}")
            return {}
    return {}


def save_seen_states(states):
    try:
        with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(states, f, indent=2)
    except Exception as e:
        print(f"Error saving seen states: {e}")


def send_discord_notification(listing):
    if not WEBHOOK_URL:
        print("Webhook URL not found. Skipping Discord notification.")
        print(
            f"Found new listing: {safe_console_text(listing.get('name'))} - {listing.get('price', {}).get('amount')} kr."
        )
        return False

    name = listing.get("name", "Unknown Apartment")
    link = listing.get("url") or f"https://udlejning.cej.dk/boliger/{listing.get('id', '')}"
    source = listing.get("source", "CEJ")
    
    mention, allowed_mentions = build_discord_mention()
    mention_prefix = f"{mention} " if mention else ""

    message = {
        "content": f"{mention_prefix}:rotating_light: **[{source}] New Apartment Alert!** :rotating_light:",
        "embeds": [
            {
                "title": build_embed_title(name, source),
                "url": link,
                "color": 3447003,  # Blue
                "fields": build_listing_fields(listing),
                "footer": {"text": f"{source} Udlejning Watcher - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
            }
        ],
    }
    if allowed_mentions:
        message["allowed_mentions"] = allowed_mentions

    if post_discord_payload(message):
        print(f"Successfully sent Discord notification for {safe_console_text(name)}")
        return True
    return False


def extract_json_from_remix(raw_data):
    """
    Remix deferred responses output multiple lines of JSON.
    We need to extract the line that contains our searchResponse payload.
    """
    try:
        data = json.loads(raw_data)
        if "items" in data.get("searchResponse", {}):
            return data
    except json.JSONDecodeError:
        pass

    lines = raw_data.split("\n")
    for line in lines:
        if line.startswith("data:{"):
            try:
                json_str = line[5:]
                data = json.loads(json_str)
                if "searchResponse" in data and "items" in data["searchResponse"]:
                    return data
            except json.JSONDecodeError:
                pass
    return None


def fetch_url_text(url, accept="text/html,application/xhtml+xml,application/xml"):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": accept,
            "Accept-Language": HEADERS["Accept-Language"],
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise WatcherError(f"Unexpected HTTP status {response.status} for {url}")
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url):
    body = fetch_url_text(url, accept="application/json,text/javascript,*/*")
    return json.loads(body)


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json,text/javascript,*/*",
            "Accept-Language": HEADERS["Accept-Language"],
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise WatcherError(f"Unexpected HTTP status {response.status} for {url}")
        return json.loads(response.read().decode("utf-8", errors="replace"))


def strip_html_tags(value):
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def extract_price_amount(raw_value):
    return extract_amount(raw_value)


def extract_postal_code(text):
    return extract_postcode(text)


def contains_excluded_location(text):
    normalized = normalize_search_text(text)
    return any(keyword in normalized for keyword in EXCLUDED_LOCATION_KEYWORDS)


def matches_cej_location_and_price(location_text, price_amount):
    if price_amount is not None and price_amount > CEJ_MAX_PRICE:
        return False

    normalized = normalize_search_text(location_text)
    if contains_excluded_location(normalized):
        return False

    post_code = extract_postal_code(normalized)
    if post_code is not None and is_target_postal_code(post_code):
        return True

    return any(keyword in normalized for keyword in CEJ_LOCATION_KEYWORDS)


def matches_general_listing_filters(listing):
    return listing_matches_policy(listing)


def is_capital_target_location(location_text):
    normalized = normalize_search_text(location_text)
    return "kobenhavn v" in normalized or "frederiksberg" in normalized


def is_juliliving_target_location(location_text):
    return "kobenhavn k" in normalize_search_text(location_text)


def is_cwobel_target_location(location_text):
    return "islands brygge" in normalize_search_text(location_text)


def parse_retry_after_seconds(headers):
    if not headers:
        return None

    retry_after = headers.get("Retry-After")
    if retry_after is None:
        return None

    try:
        return max(0, int(float(str(retry_after).strip())))
    except ValueError:
        return None


def calculate_cej_retry_delay(attempt, base_delay_seconds, retry_after_seconds=None):
    if retry_after_seconds is not None:
        return retry_after_seconds
    return base_delay_seconds * (2 ** (attempt - 1))


def is_cej_transient_error(error):
    if isinstance(error, CEJRateLimitError):
        return True
    msg = str(error).lower()
    return "rate limited" in msg or "503" in msg or "unavailable" in msg


def fetch_cej_apartments(max_attempts=None, base_delay_seconds=None):
    print(f"[{datetime.now().isoformat()}] Fetching CEJ API...")
    max_attempts = max_attempts if max_attempts is not None else CEJ_MAX_ATTEMPTS
    base_delay_seconds = base_delay_seconds if base_delay_seconds is not None else CEJ_RETRY_BASE_SECONDS

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(API_URL, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status != 200:
                    raise WatcherError(f"CEJ API returned unexpected HTTP status: {response.status}")
                raw_data = response.read().decode("utf-8")
                break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                if attempt >= max_attempts:
                    if e.code == 429:
                        raise CEJRateLimitError(f"CEJ API rate limited after {max_attempts} attempts.") from e
                    raise WatcherError(f"CEJ API unavailable (503) after {max_attempts} attempts.") from e

                retry_after_seconds = parse_retry_after_seconds(e.headers)
                sleep_for = calculate_cej_retry_delay(attempt, base_delay_seconds, retry_after_seconds)
                reason = "rate-limited" if e.code == 429 else "unavailable (503)"
                print(f"CEJ {reason} (attempt {attempt}/{max_attempts}), retrying in {sleep_for}s.")
                time.sleep(sleep_for)
                continue
            raise WatcherError(f"Error fetching CEJ API: {e}") from e
        except urllib.error.URLError as e:
            raise WatcherError(f"Error fetching CEJ API: {e}") from e
        except Exception as e:
            raise WatcherError(f"Unexpected fetch error: {e}") from e

    data = extract_json_from_remix(raw_data)
    if not data:
        raise WatcherError("Could not parse CEJ API response (searchResponse missing).")

    search_response = data.get("searchResponse")
    if not isinstance(search_response, dict):
        raise WatcherError("CEJ API response missing 'searchResponse' object.")

    items = search_response.get("items")
    if not isinstance(items, list):
        raise WatcherError("CEJ API response missing 'items' list.")

    return items


def is_city_apartment_target_area(text):
    """Return whether text contains a postcode covered by the shared policy."""
    return is_preferred_postcode(extract_postcode(text))


def _extract_city_apartment_postcode(text):
    postcodes = {
        int(code)
        for code in re.findall(
            r"\b(?:postnummer|post\s+nr)\s+(\d{4})\b",
            normalize_text(text),
        )
    }
    return next(iter(postcodes)) if len(postcodes) == 1 else None


def fetch_city_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching City Apartment...")
    req = urllib.request.Request(
        "https://cityapartment.dk/da/lejeboliger-koebenhavn/",
        headers=CITY_APARTMENT_HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                print(f"City Apartment API returned HTTP {response.status}")
                return []
            html = response.read().decode("utf-8")
    except Exception as e:
        print(f"Error fetching City Apartment: {e}")
        return []

    return parse_city_apartment_listings(html)


def parse_city_apartment_listings(html):
    apartments = []
    # Scope to the actual listing cards ("cityapartments" custom post type).
    # The page also wraps its whole body in an unrelated outer <article> (the
    # WordPress page shell), which a naive `<article>...</article>` regex would
    # incorrectly treat as the first "listing", swallowing real cards into it.
    articles = re.findall(
        r'<article[^>]*class="[^"]*\bcityapartments\b[^"]*"[^>]*>(.*?)</article>',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    for article in articles:
        title_match = re.search(r'<h[234][^>]*>(.*?)</h[234]>', article, re.DOTALL | re.IGNORECASE)
        if not title_match:
            continue
        title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()

        if "Lejeboliger" in title or "Søgeresultater" in title:
            continue

        hrefs = re.findall(r'href=["\']([^"\']+)["\']', article, re.IGNORECASE)
        link = ""
        for h in hrefs:
            if h.startswith("http") and h != "#":
                link = h
                break

        if not link:
            continue

        text = re.sub(r'<[^>]+>', ' ', article)
        text = re.sub(r'\s+', ' ', text).strip()

        post_code = _extract_city_apartment_postcode(text)
        if not is_preferred_postcode(post_code):
            continue

        price_match = re.search(r'([\d\.]+)\s*DK', text)
        price = price_match.group(1).replace('.', '') if price_match else "Unknown"

        apartments.append({
            "id": link,
            "status": "Available",
            "name": title,
            "price": {"amount": price},
            "location": {"formatted": f"Post nr. {post_code}"},
            "availableFrom": "See link for info",
            "url": link,
            "source": "City Apartment"
        })

    return apartments


def fetch_capitalbolig_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching Capital Bolig...")
    apartments = []
    try:
        items = fetch_json(CAPITALBOLIG_API_URL)
    except Exception as e:
        print(f"Error fetching Capital Bolig: {e}")
        return apartments

    if not isinstance(items, list):
        return apartments

    for item in items:
        if not isinstance(item, dict):
            continue

        listing_id = item.get("id")
        link = item.get("link")
        title = strip_html_tags((item.get("title") or {}).get("rendered"))

        if not listing_id or not link or not title:
            continue
        if not is_capital_target_location(title):
            continue

        detail_lines = []
        try:
            detail_lines = extract_html_text_lines(fetch_url_text(link))
        except Exception as e:
            print(f"Error fetching Capital Bolig detail page for {safe_console_text(title)}: {e}")

        price_amount = extract_price_amount(extract_labeled_text_value(detail_lines, "Husleje"))
        size_sqm = extract_numeric_value(extract_labeled_text_value(detail_lines, "Antal m2"))
        rooms = extract_numeric_value(extract_labeled_text_value(detail_lines, "Antal rum"))
        available_from = extract_labeled_text_value(detail_lines, "Overtagelsesdato") or "See link for info"

        apartments.append(
            {
                "id": f"capital:{listing_id}",
                "status": "Available",
                "name": title,
                "price": {"amount": price_amount if price_amount is not None else "Unknown"},
                "location": {"formatted": title},
                "availableFrom": available_from,
                "url": link,
                "source": "Capital Bolig",
                "size_sqm": size_sqm,
                "rooms": rooms,
            }
        )

    return apartments


def fetch_juliliving_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching Juli Living...")
    apartments = []

    try:
        page_html = fetch_url_text(JULILIVING_PAGE_URL)
    except Exception as e:
        print(f"Error fetching Juli Living page: {e}")
        return apartments

    ajax_url = JULILIVING_AJAX_FALLBACK_URL
    data_match = re.search(r"var\s+jlet_data\s*=\s*(\{.*?\});", page_html, re.DOTALL)
    if data_match:
        try:
            config = json.loads(data_match.group(1).replace("\\/", "/"))
            ajax_url = config.get("ajaxUrl") or ajax_url
        except json.JSONDecodeError:
            pass

    units_url = f"{ajax_url}?action=jlet_units&locale=da"
    try:
        payload = fetch_json(units_url)
    except Exception as e:
        print(f"Error fetching Juli Living units feed: {e}")
        return apartments

    units = ((payload.get("data") or {}).get("units")) if isinstance(payload, dict) else None
    if not isinstance(units, list):
        return apartments

    for unit in units:
        if not isinstance(unit, dict):
            continue

        zip_city = unit.get("ZipCity") or ""
        if not zip_city and unit.get("postcode"):
            zip_city = f"{unit.get('postcode')} {unit.get('city', '')}".strip()

        if not is_juliliving_target_location(zip_city):
            continue

        unit_id = unit.get("id") or unit.get("url")
        if not unit_id:
            continue

        address = (unit.get("address") or "").strip()
        location_text = ", ".join(part for part in [address, zip_city] if part)
        price_amount = extract_price_amount(unit.get("RentPerMonth") or unit.get("price"))

        apartments.append(
            {
                "id": f"juliliving:{unit_id}",
                "status": normalize_listing_status(unit.get("StatusText") or "Available"),
                "name": unit.get("Headline") or address or "Juli Living listing",
                "price": {"amount": price_amount if price_amount is not None else "Unknown"},
                "location": {"formatted": location_text or "København K"},
                "availableFrom": unit.get("VacantDate") or "See link for info",
                "url": unit.get("url") or JULILIVING_PAGE_URL,
                "source": "Juli Living",
                "size_sqm": extract_numeric_value(unit.get("SquareMeters") or unit.get("area")),
                "rooms": extract_numeric_value(unit.get("rooms") or unit.get("nofRooms")),
            }
        )

    return apartments


def fetch_cwobel_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching C.W. Obel...")
    apartments = []

    try:
        html = fetch_url_text(CWOBEL_ISLANDS_BRYGGE_URL)
    except Exception as e:
        print(f"Error fetching C.W. Obel area page: {e}")
        return apartments

    row_re = re.compile(
        r"<tr\s+data-estate-name=\"(?P<title>[^\"]+)\"[^>]*?"
        r"onclick=\"window\.location\s*=\s*'(?P<link>https://www\.cwobel-ejendomme\.dk/bolig/lejemaal/(?P<slug>[^/]+)/)'\"[^>]*>"
        r"(?P<body>.*?)</tr>",
        re.IGNORECASE | re.DOTALL,
    )

    for match in row_re.finditer(html):
        title = strip_html_tags(match.group("title"))
        body = match.group("body")
        link = match.group("link")
        slug = match.group("slug")

        location_match = re.search(r"<td class=\"text-left[^\"]*\">(.*?)</td>", body, re.IGNORECASE | re.DOTALL)
        location_text = strip_html_tags(location_match.group(1) if location_match else "")
        if not is_cwobel_target_location(f"{title} {location_text}"):
            continue

        price_amount = None
        price_sort_match = re.search(r"data-sort=\"(\d+)\"", body)
        if price_sort_match:
            price_amount = extract_price_amount(price_sort_match.group(1))

        apartments.append(
            {
                "id": f"cwobel:{slug}",
                "status": "Available",
                "name": title,
                "price": {"amount": price_amount if price_amount is not None else "Unknown"},
                "location": {"formatted": location_text or "Islands Brygge"},
                "availableFrom": "See link for info",
                "url": link,
                "source": "C.W. Obel",
            }
        )

    return apartments


def fetch_propstep_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching Propstep...")
    apartments = []
    page_size = 100
    max_pages = 20

    for page in range(1, max_pages + 1):
        payload = {
            "country": "DK",
            "transactionType": 1,
            "pageSize": page_size,
            "page": page,
            "waitingLists": False,
            "isLocationEnabled": False,
        }

        try:
            data = post_json(PROPSTEP_SEARCH_URL, payload)
        except Exception as e:
            print(f"Error fetching Propstep page {page}: {e}")
            break

        search_results = data.get("searchResults") if isinstance(data, dict) else None
        if not isinstance(search_results, list) or not search_results:
            break

        for group in search_results:
            if not isinstance(group, dict):
                continue
            properties = group.get("properties")
            if not isinstance(properties, list):
                continue

            for prop in properties:
                if not isinstance(prop, dict):
                    continue

                prop_id = prop.get("id") or prop.get("slug")
                if not prop_id:
                    continue

                location = prop.get("location") or {}
                address = location.get("address") or prop.get("name") or ""
                city = location.get("city") or ""
                postal_code = location.get("postalcode") or ""
                location_text = ", ".join(part for part in [address, f"{postal_code} {city}".strip()] if part)

                price_cents = (prop.get("transactionDetails") or {}).get("price")
                price_amount = int(price_cents / 100) if isinstance(price_cents, (int, float)) else None

                if not matches_cej_location_and_price(location_text, price_amount):
                    continue

                slug = prop.get("slug") or str(prop_id)
                effective_status = prop.get("transactionStatus")
                if effective_status is None:
                    effective_status = prop.get("status")
                property_details = prop.get("propertyDetails") or {}

                apartments.append(
                    {
                        "id": f"propstep:{prop_id}",
                        "status": normalize_listing_status(effective_status),
                        "name": prop.get("name") or address or "Propstep listing",
                        "price": {"amount": price_amount if price_amount is not None else "Unknown"},
                        "location": {"formatted": location_text or "Unknown location"},
                        "availableFrom": (prop.get("transactionDetails") or {}).get("availableFrom") or "See link for info",
                        "url": f"https://propstep.com/da-DK/soeg?slug={slug}",
                        "source": "Propstep",
                        "size_sqm": extract_numeric_value(property_details.get("size")),
                        "rooms": extract_numeric_value(property_details.get("rooms")),
                    }
                )

        total_properties = data.get("totalProperties") if isinstance(data, dict) else None
        if isinstance(total_properties, int) and page * page_size >= total_properties:
            break

    return apartments


def fetch_sweet_homes_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching Sweet Homes...")
    apartments = []

    try:
        html = fetch_url_text(SWEET_HOMES_LIST_URL)
    except Exception as e:
        print(f"Error fetching Sweet Homes: {e}")
        return apartments

    card_re = re.compile(
        r"<div data-elementor-type=\"loop-item\"[^>]*?post-(?P<post_id>\d+)\s+lejebolig[\s\S]*?"
        r"<a[^>]+href=\"(?P<link>https://sweet-homes\.dk/lejebolig/[^\"]+)\"[^>]*>(?P<body>[\s\S]*?)</a>\s*</div>",
        re.IGNORECASE,
    )

    for match in card_re.finditer(html):
        post_id = match.group("post_id")
        link = match.group("link")
        body = match.group("body")

        headings = re.findall(r"<h2[^>]*>(.*?)</h2>", body, flags=re.IGNORECASE | re.DOTALL)
        heading_text = [strip_html_tags(value) for value in headings if strip_html_tags(value)]
        address = heading_text[0] if heading_text else "Sweet Homes listing"
        city = heading_text[1] if len(heading_text) > 1 else ""

        custom_texts = [
            strip_html_tags(value)
            for value in re.findall(
                r"elementor-post-info__item--type-custom\">\s*([^<]+?)\s*</span>",
                body,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        price_text = next((text for text in custom_texts if "leje" in text.lower()), "")
        price_amount = extract_price_amount(price_text)

        location_text = ", ".join(part for part in [address, city] if part)
        if not matches_cej_location_and_price(location_text, price_amount):
            continue

        status = "Available"
        status_text = " ".join(custom_texts).lower()
        if "udlejet" in status_text:
            status = "Rented"
        elif "reserveret" in status_text:
            status = "Reserved"

        apartments.append(
            {
                "id": f"sweethomes:{post_id}",
                "status": status,
                "name": address,
                "price": {"amount": price_amount if price_amount is not None else "Unknown"},
                "location": {"formatted": location_text or "Unknown location"},
                "availableFrom": "See link for info",
                "url": link,
                "source": "Sweet Homes",
            }
        )

    return apartments


def fetch_fast_source_apartments():
    """Fetch the cheap-to-poll sources: CEJ itself, plus the sibling sources
    bundled into this Mixed Watcher that are similarly cheap (a single request
    or a small handful) -- Propstep, Sweet Homes, and City Apartment. These are
    fetched on every adaptive-polling cycle."""
    all_items = []

    try:
        all_items.extend(normalize_cej_listing(item) for item in fetch_cej_apartments())
    except WatcherError as e:
        if not is_cej_transient_error(e):
            raise
        print(f"Skipping CEJ listings for this run: {e}")

    try:
        all_items.extend(fetch_city_apartments())
    except Exception as e:
        print(f"Error parsing City Apartment listings: {e}")

    try:
        all_items.extend(fetch_propstep_apartments())
    except Exception as e:
        print(f"Error parsing Propstep listings: {e}")

    try:
        all_items.extend(fetch_sweet_homes_apartments())
    except Exception as e:
        print(f"Error parsing Sweet Homes listings: {e}")

    return all_items


def fetch_slow_source_apartments():
    """Fetch the sources that are either slow (Juli Living) or amplify into
    many extra HTTP requests per poll (Capital Bolig fetches one detail page
    per matching listing). Polled on a much slower, independent cadence."""
    all_items = []

    try:
        all_items.extend(fetch_capitalbolig_apartments())
    except Exception as e:
        print(f"Error parsing Capital Bolig listings: {e}")

    try:
        all_items.extend(fetch_juliliving_apartments())
    except Exception as e:
        print(f"Error parsing Juli Living listings: {e}")

    try:
        all_items.extend(fetch_cwobel_apartments())
    except Exception as e:
        print(f"Error parsing C.W. Obel listings: {e}")

    return all_items


def fetch_apartments():
    """Fetch every source in one shot (fast + slow). Used by the legacy
    single-shot mode (WATCHER_RUNS > 0) and by tests; the continuous adaptive
    loop in main() calls fetch_fast_source_apartments()/fetch_slow_source_apartments()
    independently instead so the two groups can run on different cadences."""
    return fetch_fast_source_apartments() + fetch_slow_source_apartments()


def process_apartments(apartments, seen_states):
    """Evaluate a batch of fetched listings against seen_states, sending
    Discord notifications for new listings or status changes. Returns
    (sent_notifications, notification_failures)."""
    sent_notifications = 0
    notification_failures = 0

    for apt in apartments:
        if not isinstance(apt, dict):
            raise WatcherError("Apartment source returned an item in an unexpected format.")

        if not matches_general_listing_filters(apt):
            continue

        apt_id = apt.get("id")
        status = apt.get("status")

        if not apt_id:
            continue

        previous_status = seen_states.get(apt_id)

        # Upgrade legacy status silently without sending a notification
        if previous_status == "unknown":
            seen_states[apt_id] = status
            save_seen_states(seen_states)
            continue

        # Notify if completely new OR if the status changed (e.g. reserved -> available)
        if previous_status is None or previous_status != status:
            reason = "New apartment found" if previous_status is None else f"Status changed ({previous_status} -> {status})"
            print(f"{reason}: {safe_console_text(apt.get('name'))} ({apt_id})")

            if send_discord_notification(apt):
                seen_states[apt_id] = status
                save_seen_states(seen_states)
                sent_notifications += 1
            else:
                notification_failures += 1

    return sent_notifications, notification_failures


def run_check():
    """Single-shot check across every source (legacy WATCHER_RUNS mode)."""
    seen_states = load_seen_states()
    apartments = fetch_apartments()

    print(f"Found {len(apartments)} total apartments in the response.")
    _sent, notification_failures = process_apartments(apartments, seen_states)

    if notification_failures:
        raise WatcherError(f"Failed to send {notification_failures} Discord notification(s).")


def run_legacy_fixed_interval_mode():
    """Legacy behavior: a fixed number of polls at a fixed interval, all
    sources every time. Only used when WATCHER_RUNS is explicitly set > 0."""
    for run_number in range(1, RUN_COUNT + 1):
        print(f"--- Starting Run {run_number} ---")
        run_check()

        if run_number < RUN_COUNT:
            print(f"\nWaiting {SLEEP_SECONDS} seconds before Run {run_number + 1}...")
            time.sleep(SLEEP_SECONDS)

    print("\nWatcher finished successfully.")


def run_adaptive_continuous_mode():
    """Continuous loop: fast sources (CEJ, Propstep, Sweet Homes, City
    Apartment) are polled every cycle at a Copenhagen-time-aware adaptive
    interval; slow/heavy sources (Capital Bolig, Juli Living, C.W. Obel) are
    polled on their own, much slower, independent cadence. Runs until
    MAX_RUNTIME_SECONDS is reached (leaving EXIT_BUFFER_SECONDS to persist
    state), then exits cleanly so the next scheduled job can take over."""
    seen_states = load_seen_states()

    start_time = time.monotonic()
    deadline = start_time + MAX_RUNTIME_SECONDS - EXIT_BUFFER_SECONDS
    next_slow_fetch_monotonic = start_time  # fetch slow sources on the very first cycle
    run_num = 0
    total_notification_failures = 0

    while True:
        run_num += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"Deadline reached after {run_num - 1} polls. Exiting cleanly.")
            break

        local_now = copenhagen_now()
        tier = classify_period(local_now) if ADAPTIVE_POLLING else "FIXED"
        print(f"--- Poll {run_num} [{tier}] - CPH {local_now.strftime('%a %H:%M:%S')} - {remaining / 60:.1f}min remaining ---")

        apartments = fetch_fast_source_apartments()

        fetch_slow_now = time.monotonic() >= next_slow_fetch_monotonic
        if fetch_slow_now:
            apartments += fetch_slow_source_apartments()
            next_slow_fetch_monotonic = time.monotonic() + SLOW_SOURCE_INTERVAL_SECONDS

        print(f"Fetched {len(apartments)} apartments this cycle (slow sources included: {fetch_slow_now}).")
        sent, failures = process_apartments(apartments, seen_states)
        total_notification_failures += failures
        if sent == 0 and failures == 0:
            print("No new unseen apartments found in this cycle.")

        remaining = deadline - time.monotonic()
        interval = get_poll_interval_seconds(local_now)
        if remaining <= interval:
            print(f"Not enough time for another cycle ({remaining:.0f}s left, next poll would be in {interval}s). Exiting cleanly.")
            break

        print(f"Sleeping {interval}s (tier {tier}).")
        time.sleep(interval)

    total_elapsed = time.monotonic() - start_time
    print(f"Watcher finished: {run_num} polls over {total_elapsed / 60:.1f} minutes.")

    if total_notification_failures:
        raise WatcherError(f"Failed to send {total_notification_failures} Discord notification(s) across the run.")


def main():
    if RUN_COUNT > 0:
        print(f"WATCHER_RUNS={RUN_COUNT} set: using legacy fixed-interval single-shot mode.")
        run_legacy_fixed_interval_mode()
        return

    run_adaptive_continuous_mode()


if __name__ == "__main__":
    try:
        main()
    except WatcherError as e:
        print(f"Watcher failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unhandled watcher failure: {e}")
        sys.exit(1)
