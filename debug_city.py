import urllib.request
import re
import json

req = urllib.request.Request(
    "https://cityapartment.dk/da/lejeboliger-koebenhavn/",
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml"
    }
)
try:
    with urllib.request.urlopen(req, timeout=30) as response:
        html = response.read().decode("utf-8")
except Exception as e:
    with open("debug_out.txt", "w", encoding="utf-8") as f:
        f.write(f"Error fetching City Apartment: {e}")
    exit()

apartments = []
articles = re.findall(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)

with open("debug_out.txt", "w", encoding="utf-8") as f:
    f.write(f"Found {len(articles)} articles.\n")

    for i, article in enumerate(articles):
        f.write(f"\n--- Article {i+1} ---\n")
        
        title_match = re.search(r'<h[234][^>]*>(.*?)</h[234]>', article, re.DOTALL | re.IGNORECASE)
        if not title_match:
            f.write("No title match\n")
            continue

        title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
        f.write(f"Title: {title}\n")

        if "Lejeboliger" in title or "Søgeresultater" in title:
            f.write("Skipped due to title\n")
            continue

        hrefs = re.findall(r'href=["\']([^"\']+)["\']', article, re.IGNORECASE)
        link = ""
        for h in hrefs:
            if h.startswith("http") and h != "#":
                link = h
                break
        
        f.write(f"Hrefs found: {hrefs}\n")
        f.write(f"Chosen link: {link}\n")

        if not link:
            f.write("Skipped due to no valid link\n")
            continue

        text = re.sub(r'<[^>]+>', ' ', article)
        text = re.sub(r'\s+', ' ', text).strip()
        
        price_match = re.search(r'([\d\.]+)\s*DK', text)
        price = price_match.group(1).replace('.', '') if price_match else "Unknown"

        apartments.append({
            "id": link,
            "name": title,
            "price": price,
        })

    f.write(f"\nFound {len(apartments)} apartments: {json.dumps(apartments, indent=2)}\n")
