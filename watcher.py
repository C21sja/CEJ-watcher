import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlencode

from housing_policy import (
    canonical_listing_key,
    contains_commercial_use,
    contains_restricted_eligibility,
    deduplicate_listings,
    extract_amount,
    extract_postcode,
    is_preferred_postcode,
    listing_matches_policy,
    normalize_text,
)
from housing_sources import SourceContractError, SourceSnapshot, SourceSpec
from housing_sources.brikk import fetch_brikk
from housing_sources.findbolig import fetch_findbolig, make_findbolig_transport
from housing_sources.kobenhavn_dk import fetch_kobenhavn
from housing_sources.landlords import fetch_lejeboligmaegleren, fetch_norhjem, fetch_taurus
from housing_sources.readiness import fetch_cphomes, fetch_rle, fetch_vaernedamsvej

# Configurations
API_URL = "https://udlejning.cej.dk/find-bolig/overblik?collection=residences&monthlyPrice=0-50000&p=sj%C3%A6lland&_data=routes%2Fsearch%2Flayout"
CAPITALBOLIG_API_URL = "https://capitalbolig.dk/wp-json/wp/v2/bolig?per_page=100&_fields=id,link,title"
JULILIVING_PAGE_URL = "https://juliliving.dk/find-lejebolig/"
JULILIVING_AJAX_FALLBACK_URL = "https://juliliving.dk/wp-admin/admin-ajax.php"
CWOBEL_ISLANDS_BRYGGE_URL = "https://www.cwobel-ejendomme.dk/bolig/ledige-lejemaal/storkoebenhavn/islands-brygge/"
PROPSTEP_SEARCH_URL = "https://app.propstep.com/api/search"
AKF_PROPSTEP_COMPANY_ID = "5db6d00f4e5146201ae72ada"
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

# Sources are now scheduled per-source via make_source_registry() and
# fetch_due_sources() rather than two hard-coded name groups. "fast" sources
# (CEJ and its cheap siblings, plus Findbolig/Lejeboligmægleren/Norhjem) run
# every cycle at the tier-driven interval below; "ten_minute" and
# "thirty_minute" cadences cover slower or request-amplifying sources.
SLOW_SOURCE_INTERVAL_SECONDS = max(60, read_non_negative_int_env("WATCHER_SLOW_SOURCE_INTERVAL_SECONDS", 600))
READINESS_SOURCE_INTERVAL_SECONDS = max(
    300, read_non_negative_int_env("WATCHER_READINESS_SOURCE_INTERVAL_SECONDS", 1800)
)


def cadence_seconds(cadence):
    if cadence == "ten_minute":
        return SLOW_SOURCE_INTERVAL_SECONDS
    if cadence == "thirty_minute":
        return READINESS_SOURCE_INTERVAL_SECONDS
    if cadence == "fast":
        return 0
    raise ValueError(f"Unknown source cadence: {cadence}")

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


def format_price_for_display(raw_price, price_period="month"):
    price_amount = extract_price_amount(raw_price)
    if price_amount is None:
        text = str(raw_price).strip()
        return text or "Unknown"
    formatted_amount = f"{price_amount:,}".translate(str.maketrans(",.", ".,"))
    if price_period == "total":
        return f"{formatted_amount} kr."
    return f"{formatted_amount} kr/month"


def build_listing_fields(listing):
    fields = [
        {"name": "Status", "value": str(listing.get("status", "unknown")), "inline": True},
        {
            "name": "Price",
            "value": format_price_for_display(
                listing.get("price", {}).get("amount", "Unknown"),
                listing.get("price_period", "month"),
            ),
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


BASELINE_STATE_PREFIX = "__meta__:baseline:"
BASELINE_CHUNK_STATE_PREFIX = "__meta__:baseline-chunk:"
READINESS_STATE_PREFIX = "__meta__:readiness:"
CANONICAL_LISTING_STATE_PREFIX = "__meta__:listing:"
BASELINE_MENTION_STATE_KEY = "__meta__:baseline-mention"


def baseline_state_key(source):
    return f"{BASELINE_STATE_PREFIX}{source}"


def readiness_state_key(event_id):
    return f"{READINESS_STATE_PREFIX}{event_id}"


def baseline_chunk_state_key(body):
    return f"{BASELINE_CHUNK_STATE_PREFIX}{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def listing_state_key(listing):
    canonical_key = listing.get("canonical_key")
    if canonical_key:
        digest = hashlib.sha256(str(canonical_key).encode("utf-8")).hexdigest()
        return f"{CANONICAL_LISTING_STATE_PREFIX}{digest}"
    return listing.get("id")


def remember_listing_state(seen_states, listing):
    state_key = listing_state_key(listing)
    status = listing.get("status")
    if state_key:
        seen_states[state_key] = status
    if listing.get("id"):
        seen_states[listing["id"]] = status


def is_active_baseline_listing(listing):
    return normalize_text(listing.get("status")) in {"available", "ledig", "under opsigelse"}


def prepare_source_snapshots(snapshots):
    eligible = []
    for snapshot in snapshots:
        for listing in snapshot.listings:
            if listing_matches_policy(listing):
                eligible.append(listing)
    selected_object_ids = {id(listing) for listing in deduplicate_listings(eligible)}
    return [
        SourceSnapshot(
            source=snapshot.source,
            listings=[listing for listing in snapshot.listings if id(listing) in selected_object_ids],
            events=list(snapshot.events),
            diagnostics=list(snapshot.diagnostics),
        )
        for snapshot in snapshots
    ]


def _readiness_state(event):
    return {
        "signature": str(event.get("signature", "")),
        "registration_closed": bool(event.get("registration_closed", False)),
        "signals": sorted(set(event.get("signals") or [])),
    }


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


def post_form_json(url, payload):
    body = urlencode(payload, doseq=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_findbolig_live():
    fetch_text, post_search = make_findbolig_transport(HEADERS)
    return fetch_findbolig(fetch_text, post_search)


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

                company_id = prop.get("companyId") or group.get("companyId")
                is_akf = company_id == AKF_PROPSTEP_COMPANY_ID
                restricted_text = ""
                if is_akf:
                    if prop.get("transactionStatus") != 1:
                        continue
                    is_waitlist = any(
                        is_truthy(value)
                        for value in (
                            prop.get("waitingList"),
                            prop.get("isWaitingList"),
                            property_details.get("waitingList"),
                            group.get("waitingList"),
                        )
                    )
                    if is_waitlist:
                        continue
                    restricted_text = " ".join(
                        str(value or "")
                        for value in (
                            property_details.get("onlyFor"),
                            property_details.get("langToDescription"),
                            prop.get("description"),
                            prop.get("langToDescription"),
                            group.get("description"),
                            group.get("langToDescription"),
                        )
                    )
                    if contains_restricted_eligibility(restricted_text):
                        continue
                source_name = "AKF via Propstep" if is_akf else "Propstep"

                apartments.append(
                    {
                        "id": f"propstep:{prop_id}",
                        "status": normalize_listing_status(effective_status),
                        "name": prop.get("name") or address or "Propstep listing",
                        "price": {"amount": price_amount if price_amount is not None else "Unknown"},
                        "location": {"formatted": location_text or "Unknown location"},
                        "availableFrom": (prop.get("transactionDetails") or {}).get("availableFrom") or "See link for info",
                        "url": f"https://propstep.com/da-DK/soeg?slug={slug}",
                        "source": source_name,
                        "size_sqm": extract_numeric_value(property_details.get("size")),
                        "rooms": extract_numeric_value(property_details.get("rooms")),
                        "raw_text": restricted_text,
                        "canonical_key": canonical_listing_key(location_text, "rent"),
                        "source_priority": 20,
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


def make_source_registry():
    """Every tracked source, its polling cadence, and its fetch function.

    AKF is intentionally absent here: it is classified inside the single
    existing Propstep response (see fetch_propstep_apartments()), so no
    second HTTP request or duplicate ID is created for it. The logical
    "AKF via Propstep" baseline snapshot is derived from that response by
    build_logical_baseline_snapshots() below.
    """
    return [
        SourceSpec(
            "CEJ",
            "fast",
            lambda: SourceSnapshot("CEJ", [normalize_cej_listing(item) for item in fetch_cej_apartments()]),
            baseline=False,
        ),
        SourceSpec("City Apartment", "fast", lambda: SourceSnapshot("City Apartment", fetch_city_apartments()), baseline=False),
        SourceSpec("Propstep", "fast", lambda: SourceSnapshot("Propstep", fetch_propstep_apartments()), baseline=False),
        SourceSpec("Sweet Homes", "fast", lambda: SourceSnapshot("Sweet Homes", fetch_sweet_homes_apartments()), baseline=False),
        SourceSpec("Findbolig", "fast", fetch_findbolig_live),
        SourceSpec("Lejeboligmægleren", "fast", lambda: fetch_lejeboligmaegleren(post_json, fetch_json)),
        SourceSpec("Norhjem", "fast", lambda: fetch_norhjem(post_form_json, fetch_url_text)),
        SourceSpec("Capital Bolig", "ten_minute", lambda: SourceSnapshot("Capital Bolig", fetch_capitalbolig_apartments()), baseline=False),
        SourceSpec("Juli Living", "ten_minute", lambda: SourceSnapshot("Juli Living", fetch_juliliving_apartments()), baseline=False),
        SourceSpec("C.W. Obel", "ten_minute", lambda: SourceSnapshot("C.W. Obel", fetch_cwobel_apartments()), baseline=False),
        SourceSpec("Taurus", "ten_minute", lambda: fetch_taurus(fetch_url_text)),
        SourceSpec("Brikk", "ten_minute", lambda: fetch_brikk(fetch_url_text)),
        SourceSpec("Kobenhavn.dk", "ten_minute", lambda: fetch_kobenhavn(fetch_url_text)),
        SourceSpec("RLE", "ten_minute", lambda: fetch_rle(fetch_json)),
        SourceSpec("Værnedamsvej", "ten_minute", lambda: fetch_vaernedamsvej(fetch_url_text)),
        SourceSpec("CPH Homes", "thirty_minute", lambda: fetch_cphomes(fetch_url_text)),
    ]


def fetch_due_sources(registry, now, next_due):
    """Fetch every source whose cadence is due, isolating one source's
    failure from the others. Returns (snapshots, succeeded_source_names).
    A contract-valid empty snapshot counts as success; an exception does
    not produce a snapshot at all, so the baseline can tell "zero current
    matches" apart from "this source failed to fetch"."""
    snapshots = []
    succeeded = set()
    for spec in registry:
        due = spec.cadence == "fast" or now >= next_due.get(spec.name, 0.0)
        if not due:
            continue
        try:
            snapshot = spec.fetch()
            if not isinstance(snapshot, SourceSnapshot):
                raise SourceContractError(f"{spec.name} did not return a SourceSnapshot")
            if snapshot.source != spec.name:
                raise SourceContractError(
                    f"{spec.name} returned a snapshot labelled {snapshot.source}"
                )
            snapshots.append(snapshot)
            succeeded.add(spec.name)
        except Exception as exc:
            print(f"[{spec.name}] source fetch failed: {safe_console_text(exc)}")
        finally:
            if spec.cadence != "fast":
                next_due[spec.name] = now + cadence_seconds(spec.cadence)
    return snapshots, succeeded


# AKF via Propstep is a logical sub-source of Propstep's single response
# (see Task 9's classification inside fetch_propstep_apartments()): it
# needs its own baseline digest entry without triggering a second fetch.
LOGICAL_BASELINE_SUBSOURCES = {"Propstep": ("AKF via Propstep",)}


def build_logical_baseline_snapshots(snapshots, registry):
    baseline_names = {spec.name for spec in registry if spec.baseline}
    logical = [snapshot for snapshot in snapshots if snapshot.source in baseline_names]
    for snapshot in snapshots:
        for source_label in LOGICAL_BASELINE_SUBSOURCES.get(snapshot.source, ()):
            logical.append(
                SourceSnapshot(
                    source_label,
                    listings=[listing for listing in snapshot.listings if listing.get("source") == source_label],
                )
            )
    return logical


def process_source_snapshots(snapshots, registry, seen_states):
    """Run the baseline digest for any source seeing its first successful
    fetch, then send ordinary per-listing/readiness alerts for everything
    else. Returns (sent, failures, incomplete_baseline_sources)."""
    prepared = prepare_source_snapshots(snapshots)
    logical_baselines = build_logical_baseline_snapshots(prepared, registry)
    baseline_sources = {snapshot.source for snapshot in logical_baselines}
    incomplete, baseline_failures = initialize_source_baselines(logical_baselines, baseline_sources, seen_states)
    ready = [
        SourceSnapshot(
            snapshot.source,
            listings=[listing for listing in snapshot.listings if listing.get("source") not in incomplete],
            events=list(snapshot.events),
            diagnostics=list(snapshot.diagnostics),
        )
        for snapshot in prepared
        if snapshot.source not in incomplete
    ]
    listings = [listing for snapshot in ready for listing in snapshot.listings]
    events = [event for snapshot in ready for event in snapshot.events]
    listing_sent, listing_failures = process_apartments(listings, seen_states)
    event_sent, event_failures = process_readiness_events(events, seen_states)
    return listing_sent + event_sent, baseline_failures + listing_failures + event_failures, incomplete


def fetch_apartments():
    """Fetch every registered source once, without running the alert
    pipeline. Used by the legacy single-shot mode (WATCHER_RUNS > 0) and by
    tests; the continuous adaptive loop in main() calls
    fetch_due_sources()/process_source_snapshots() directly instead so each
    source can run on its own cadence."""
    registry = make_source_registry()
    snapshots, _succeeded = fetch_due_sources(registry, now=0.0, next_due={})
    prepared = prepare_source_snapshots(snapshots)
    return [listing for snapshot in prepared for listing in snapshot.listings]


def process_apartments(apartments, seen_states):
    """Evaluate a batch of fetched listings against seen_states, sending
    Discord notifications for new listings or status changes. Returns
    (sent_notifications, notification_failures)."""
    sent_notifications = 0
    notification_failures = 0
    for apartment in apartments:
        if not isinstance(apartment, dict):
            raise WatcherError("Apartment source returned an item in an unexpected format.")
        if not matches_general_listing_filters(apartment):
            continue
        apartment_id = apartment.get("id")
        status = apartment.get("status")
        if not apartment_id:
            continue
        state_key = listing_state_key(apartment)
        previous_status = seen_states.get(state_key)
        if previous_status is None and state_key != apartment_id:
            previous_status = seen_states.get(apartment_id)
            if previous_status is not None:
                seen_states[state_key] = previous_status
                save_seen_states(seen_states)
        actionable = is_active_baseline_listing(apartment)

        if previous_status == "unknown" or (previous_status is None and not actionable):
            remember_listing_state(seen_states, apartment)
            save_seen_states(seen_states)
            continue
        if previous_status == status:
            continue
        if not actionable:
            remember_listing_state(seen_states, apartment)
            save_seen_states(seen_states)
            continue

        reason = "New apartment found" if previous_status is None else f"Status changed ({previous_status} -> {status})"
        print(f"{reason}: {safe_console_text(apartment.get('name'))} ({apartment_id})")
        if send_discord_notification(apartment):
            remember_listing_state(seen_states, apartment)
            save_seen_states(seen_states)
            sent_notifications += 1
        else:
            notification_failures += 1
    return sent_notifications, notification_failures


def _compact_listing_line(listing):
    name = str(listing.get("name") or (listing.get("location") or {}).get("formatted") or "Unknown home")
    price = format_price_for_display(
        (listing.get("price") or {}).get("amount"), listing.get("price_period", "month")
    )
    status = str(listing.get("status") or "unknown")
    return f"- [{name}]({listing.get('url')}) — {price} — {status}"


def _snapshot_digest_lines(snapshot):
    active = [listing for listing in snapshot.listings if is_active_baseline_listing(listing)]
    lines = [f"**{snapshot.source}: {len(active)} active match(es)**"]
    if active:
        lines.extend(_compact_listing_line(listing) for listing in active)
    else:
        lines.append("- No active current matches.")
    for event in snapshot.events:
        headline = event.get("baseline_headline") or event.get("headline") or "Monitoring ready"
        lines.append(f"- Status: [{headline}]({event.get('url')})")
    return lines


def build_baseline_digest_chunks(snapshots, max_chars=1600):
    chunks = []
    current_lines = []
    current_sources = set()

    def flush_current():
        nonlocal current_lines, current_sources
        if current_lines:
            chunks.append({"body": "\n".join(current_lines), "sources": frozenset(current_sources)})
            current_lines = []
            current_sources = set()

    for snapshot in snapshots:
        source_lines = [
            line if len(line) <= max_chars else line[: max_chars - 1] + "…"
            for line in _snapshot_digest_lines(snapshot)
        ]
        source_body = "\n".join(source_lines)
        if len(source_body) <= max_chars:
            projected = "\n".join(current_lines + source_lines)
            if current_lines and len(projected) > max_chars:
                flush_current()
            current_lines.extend(source_lines)
            current_sources.add(snapshot.source)
            continue

        flush_current()
        source_chunk_lines = []
        for line in source_lines:
            if source_chunk_lines and len("\n".join(source_chunk_lines + [line])) > max_chars:
                chunks.append({"body": "\n".join(source_chunk_lines), "sources": frozenset({snapshot.source})})
                source_chunk_lines = []
            source_chunk_lines.append(line)
        if source_chunk_lines:
            chunks.append({"body": "\n".join(source_chunk_lines), "sources": frozenset({snapshot.source})})
    flush_current()
    return chunks


def _baseline_payload(body, include_mention):
    mention, allowed_mentions = build_discord_mention() if include_mention else (None, None)
    prefix = f"{mention} " if mention else ""
    payload = {"content": f"{prefix}:house: **Housing tracker baseline**\n{body}"}
    if allowed_mentions:
        payload["allowed_mentions"] = allowed_mentions
    return payload


def initialize_source_baselines(snapshots, baseline_source_names, seen_states, max_chars=1600):
    pending = [
        snapshot
        for snapshot in snapshots
        if snapshot.source in baseline_source_names
        and seen_states.get(baseline_state_key(snapshot.source)) != "complete"
    ]
    if not pending:
        return set(), 0
    if not WEBHOOK_URL:
        print("Webhook URL not found. Baselines remain pending.")
        return {snapshot.source for snapshot in pending}, 1

    source_succeeded = {snapshot.source: True for snapshot in pending}
    delivery_failures = 0
    include_mention = seen_states.get(BASELINE_MENTION_STATE_KEY) != "sent"
    for chunk in build_baseline_digest_chunks(pending, max_chars=max_chars):
        chunk_key = baseline_chunk_state_key(chunk["body"])
        delivered = seen_states.get(chunk_key) == "sent"
        if not delivered:
            delivered = post_discord_payload(_baseline_payload(chunk["body"], include_mention))
            if delivered:
                seen_states[chunk_key] = "sent"
                save_seen_states(seen_states)
        if delivered and include_mention:
            seen_states[BASELINE_MENTION_STATE_KEY] = "sent"
            save_seen_states(seen_states)
            include_mention = False
        if not delivered:
            delivery_failures += 1
            for source in chunk["sources"]:
                source_succeeded[source] = False

    incomplete = set()
    changed = False
    for snapshot in pending:
        if not source_succeeded[snapshot.source]:
            incomplete.add(snapshot.source)
            continue
        for listing in snapshot.listings:
            if listing.get("id"):
                remember_listing_state(seen_states, listing)
        for event in snapshot.events:
            if event.get("id"):
                seen_states[readiness_state_key(event["id"])] = _readiness_state(event)
        seen_states[baseline_state_key(snapshot.source)] = "complete"
        changed = True
    if changed:
        save_seen_states(seen_states)
    return incomplete, delivery_failures


def send_readiness_notification(event, urgent=False):
    if not WEBHOOK_URL:
        print(f"Webhook URL not found. Readiness event remains pending: {safe_console_text(event.get('headline'))}")
        return False
    kind = event.get("kind", "project_update")
    is_application_opening = kind == "application_opening"
    mention, allowed_mentions = build_discord_mention() if is_application_opening else (None, None)
    label = {
        "application_opening": "APPLICATION OPENING",
        "inspection": "Inspection needed",
        "project_update": "Project update",
    }.get(kind, "Housing source update")
    payload = {
        "content": f"{mention + ' ' if mention else ''}:rotating_light: **{label} — {event.get('source', 'Source')}**",
        "embeds": [
            {
                "title": str(event.get("headline") or label),
                "url": event.get("url"),
                "description": str(event.get("description") or "Inspect the official page.")[:4000],
                "color": 15158332 if urgent else 15844367,
            }
        ],
    }
    if allowed_mentions:
        payload["allowed_mentions"] = allowed_mentions
    return post_discord_payload(payload)


def process_readiness_events(events, seen_states):
    sent = 0
    failures = 0
    for event in events:
        event_id = event.get("id")
        signature = str(event.get("signature") or "")
        if not event_id or not signature:
            raise WatcherError("Readiness event is missing an ID or signature.")
        key = readiness_state_key(event_id)
        previous = seen_states.get(key)
        previous_signature = previous.get("signature") if isinstance(previous, dict) else previous
        if previous_signature == signature:
            continue
        previous_closed = bool(previous.get("registration_closed")) if isinstance(previous, dict) else False
        previous_signals = set(previous.get("signals") or []) if isinstance(previous, dict) else set()
        current_signals = set(event.get("signals") or [])
        registration_opened = previous_closed and not event.get("registration_closed", False)
        urgent = bool(
            event.get("urgent")
            or registration_opened
            or (current_signals - previous_signals)
        )
        alert_event = dict(event)
        if registration_opened:
            alert_event["kind"] = "application_opening"
            alert_event["headline"] = event.get("urgent_headline") or "APPLICATION OPENING"
            alert_event["url"] = event.get("application_url") or event.get("url")
        if previous is not None and event.get("change_headline"):
            alert_event["headline"] = event["change_headline"]
        if send_readiness_notification(alert_event, urgent=urgent):
            seen_states[key] = _readiness_state(event)
            save_seen_states(seen_states)
            sent += 1
        else:
            failures += 1
    return sent, failures


def run_check():
    """Single-shot check across every source (legacy WATCHER_RUNS mode)."""
    seen_states = load_seen_states()
    registry = make_source_registry()
    snapshots, _succeeded = fetch_due_sources(registry, now=0.0, next_due={})

    total_listings = sum(len(snapshot.listings) for snapshot in snapshots)
    print(f"Found {total_listings} total apartments in the response.")
    _sent, notification_failures, _incomplete = process_source_snapshots(snapshots, registry, seen_states)

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
    """Continuous loop: every registered source is polled on its own
    cadence via fetch_due_sources() ("fast" sources every cycle at a
    Copenhagen-time-aware adaptive interval; "ten_minute"/"thirty_minute"
    sources on their own, much slower, independent schedule). Runs until
    MAX_RUNTIME_SECONDS is reached (leaving EXIT_BUFFER_SECONDS to persist
    state), then exits cleanly so the next scheduled job can take over."""
    seen_states = load_seen_states()
    registry = make_source_registry()
    next_due = {}

    start_time = time.monotonic()
    deadline = start_time + MAX_RUNTIME_SECONDS - EXIT_BUFFER_SECONDS
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

        now = time.monotonic()
        snapshots, succeeded = fetch_due_sources(registry, now=now, next_due=next_due)
        print(
            f"Fetched {sum(len(snapshot.listings) for snapshot in snapshots)} listings "
            f"and {sum(len(snapshot.events) for snapshot in snapshots)} readiness states "
            f"from {len(succeeded)} successful source(s)."
        )
        sent, failures, incomplete = process_source_snapshots(snapshots, registry, seen_states)
        total_notification_failures += failures
        if incomplete:
            print(f"Pending first-success baselines: {', '.join(sorted(incomplete))}")
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
