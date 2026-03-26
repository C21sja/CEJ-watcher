import json
import os
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


class WatcherError(Exception):
    """Raised when the watcher cannot complete due to break conditions."""


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


RUN_COUNT = read_non_negative_int_env("WATCHER_RUNS", 2)
SLEEP_SECONDS = read_non_negative_int_env("WATCHER_SLEEP_SECONDS", 120)


def load_seen_ids():
    if os.path.exists(SEEN_IDS_FILE):
        try:
            with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error loading seen IDs: {e}")
            return set()
    return set()


def save_seen_ids(seen_ids):
    try:
        with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_ids), f, indent=2)
    except Exception as e:
        print(f"Error saving seen IDs: {e}")


def send_discord_notification(listing):
    if not WEBHOOK_URL:
        print("Webhook URL not found. Skipping Discord notification.")
        print(f"Found new listing: {listing.get('name')} - {listing.get('price', {}).get('amount')} kr.")
        return False

    name = listing.get("name", "Unknown Apartment")
    price = listing.get("price", {}).get("amount", "Unknown Price")
    address = listing.get("location", {}).get("formatted", "Unknown Address")
    available_from = listing.get("availableFrom", "Unknown Date")
    link = f"https://udlejning.cej.dk/find-bolig/{listing.get('id', '')}"

    message = {
        "content": ":rotating_light: **New Apartment Alert!** :rotating_light:",
        "embeds": [
            {
                "title": name,
                "url": link,
                "color": 3447003,  # Blue
                "fields": [
                    {"name": "Price", "value": f"{price} kr/month", "inline": True},
                    {"name": "Address", "value": address, "inline": True},
                    {"name": "Available From", "value": available_from, "inline": True},
                ],
                "footer": {"text": f"CEJ Udlejning Watcher - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
            }
        ],
    }

    data = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status in [200, 204]:
                print(f"Successfully sent Discord notification for {name}")
                return True
            print(f"Failed to send Discord notification. Status: {response.status}")
            return False
    except urllib.error.URLError as e:
        print(f"URL Error sending to Discord: {e}")
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


def fetch_apartments():
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


def run_check():
    seen_ids = load_seen_ids()
    apartments = fetch_apartments()

    print(f"Found {len(apartments)} total apartments in the response.")
    sent_notifications = 0
    notification_failures = 0

    for apt in apartments:
        if not isinstance(apt, dict):
            raise WatcherError("CEJ API returned apartment items in an unexpected format.")

        apt_id = apt.get("id")
        status = apt.get("status")

        # Only notify for 'available' apartments that we haven't seen yet
        if apt_id and status == "available" and apt_id not in seen_ids:
            print(f"New apartment found: {apt.get('name')} ({apt_id})")
            if send_discord_notification(apt):
                seen_ids.add(apt_id)
                # Persist each successful notification to minimize duplicate alerts
                # if a later apartment fails in the same run.
                save_seen_ids(seen_ids)
                sent_notifications += 1
            else:
                notification_failures += 1

    if notification_failures:
        raise WatcherError(f"Failed to send {notification_failures} Discord notification(s).")
    if sent_notifications == 0:
        print("No new available apartments found in this check.")


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
