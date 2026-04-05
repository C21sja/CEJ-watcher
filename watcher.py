import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Configurations
API_URL = "https://udlejning.cej.dk/find-bolig/overblik?collection=residences&monthlyPrice=0-50000&p=sj%C3%A6lland&_data=routes%2Fsearch%2Flayout"
HEADERS = {
    "x-remix-response": "yes",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
}
SEEN_IDS_FILE = "seen_ids.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "@user")
DISCORD_MENTION_USER_ID = os.environ.get("DISCORD_MENTION_USER_ID")
DISCORD_MENTION_EVERYONE = os.environ.get("DISCORD_MENTION_EVERYONE", "true")


class WatcherError(Exception):
    """Raised when the watcher cannot complete due to break conditions."""


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


RUN_COUNT = read_non_negative_int_env("WATCHER_RUNS", 28)
SLEEP_SECONDS = read_non_negative_int_env("WATCHER_SLEEP_SECONDS", 60)

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
        print(f"Found new listing: {listing.get('name')} - {listing.get('price', {}).get('amount')} kr.")
        return False

    name = listing.get("name", "Unknown Apartment")
    price = listing.get("price", {}).get("amount", "Unknown Price")
    address = listing.get("location", {}).get("formatted", "Unknown Address")
    available_from = listing.get("availableFrom", "Unknown Date")
    status = listing.get("status", "unknown")
    link = listing.get("url") or f"https://udlejning.cej.dk/boliger/{listing.get('id', '')}"
    source = listing.get("source", "CEJ")
    
    mention, allowed_mentions = build_discord_mention()
    mention_prefix = f"{mention} " if mention else ""

    message = {
        "content": f"{mention_prefix}:rotating_light: **New Apartment Alert!** :rotating_light:",
        "embeds": [
            {
                "title": name,
                "url": link,
                "color": 3447003,  # Blue
                "fields": [
                    {"name": "Status", "value": str(status), "inline": True},
                    {"name": "Price", "value": f"{price} kr/month", "inline": True},
                    {"name": "Address", "value": address, "inline": True},
                    {"name": "Available From", "value": available_from, "inline": True},
                ],
                "footer": {"text": f"{source} Udlejning Watcher - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
            }
        ],
    }
    if allowed_mentions:
        message["allowed_mentions"] = allowed_mentions

    if post_discord_payload(message):
        print(f"Successfully sent Discord notification for {name}")
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


def fetch_cej_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching CEJ API...")
    req = urllib.request.Request(API_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                raise WatcherError(f"CEJ API returned unexpected HTTP status: {response.status}")
            raw_data = response.read().decode("utf-8")
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


def fetch_city_apartments():
    print(f"[{datetime.now().isoformat()}] Fetching City Apartment...")
    req = urllib.request.Request(
        "https://cityapartment.dk/da/lejeboliger-koebenhavn/",
        headers={
            "User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0"),
            "Accept": "text/html,application/xhtml+xml,application/xml"
        }
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

    apartments = []
    articles = re.findall(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
    
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
        
        if "odense" in title.lower() or "odense" in text.lower():
            continue
        post_match = re.search(r'Post nr\. (\d{4})', text, re.IGNORECASE)
        if post_match:
            post_code = int(post_match.group(1))
            if post_code < 1000 or post_code >= 4000:
                continue
        
        price_match = re.search(r'([\d\.]+)\s*DK', text)
        price = price_match.group(1).replace('.', '') if price_match else "Unknown"

        apartments.append({
            "id": link,
            "status": "Available",
            "name": title,
            "price": {"amount": price},
            "location": {"formatted": title},
            "availableFrom": "See link for info",
            "url": link,
            "source": "City Apartment"
        })
        
    return apartments


def fetch_apartments():
    all_items = []
    
    # CEJ properties
    all_items.extend(fetch_cej_apartments())

    # City Apartment properties
    try:
        all_items.extend(fetch_city_apartments())
    except Exception as e:
        print(f"Error parsing City Apartment listings: {e}")
        
    return all_items


def run_check():
    seen_states = load_seen_states()
    apartments = fetch_apartments()

    print(f"Found {len(apartments)} total apartments in the response.")
    sent_notifications = 0
    notification_failures = 0

    for apt in apartments:
        if not isinstance(apt, dict):
            raise WatcherError("CEJ API returned apartment items in an unexpected format.")

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
            print(f"{reason}: {apt.get('name')} ({apt_id})")
            
            if send_discord_notification(apt):
                seen_states[apt_id] = status
                save_seen_states(seen_states)
                sent_notifications += 1
            else:
                notification_failures += 1

    if notification_failures:
        raise WatcherError(f"Failed to send {notification_failures} Discord notification(s).")
    if sent_notifications == 0:
        print("No new unseen apartments found in this check.")


def main():
    if RUN_COUNT == 0:
        print("WATCHER_RUNS=0, nothing to do.")
        return

    for run_number in range(1, RUN_COUNT + 1):
        print(f"--- Starting Run {run_number} ---")
        run_check()

        if run_number < RUN_COUNT:
            print(f"\nWaiting {SLEEP_SECONDS} seconds before Run {run_number + 1}...")
            time.sleep(SLEEP_SECONDS)

    print("\nWatcher finished successfully.")


if __name__ == "__main__":
    try:
        main()
    except WatcherError as e:
        print(f"Watcher failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unhandled watcher failure: {e}")
        sys.exit(1)
