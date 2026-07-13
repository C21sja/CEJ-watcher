import re
import unicodedata


GENERAL_RENT_MAX = 18_000
COOPERATIVE_SALE_MAX = 2_800_000
PREFERRED_POSTCODES = frozenset({2100, 2150, 2200, 2300, 2400, 2450})

RESTRICTED_TERMS = (
    "kun for studerende",
    "only for students",
    "studiebolig",
    "ungdomsbolig",
    "kun for unge",
    "seniorbolig",
    "kun for seniorer",
    "aeldrebolig",
    "kun for pensionister",
    "pensionskunde",
    "kun for pensionskunder",
    "pensionskunder har fortrinsret",
    "medlemskab kraeves",
    "krav om medlemskab",
    "kun for medlemmer",
    "for medlemmer af",
)

RESTRICTED_PATTERNS = (
    r"\b(?:minimum|min)\s*(?:alder)?\s*(?:55|60|65)\b",
    r"\b(?:55|60|65)\s*aar\b",
    r"\b(?:skal|forudsaetter)\s+(?:vaere\s+)?medlem\s+af\b",
    r"\b(?:kober(?:en)?\s+)?(?:bliver\s+)?medlem\s+af\s+(?:en\s+)?andelsforening(?:en)?\b",
    r"\b(?:kraever|krav\s+om)\s+medlemskab\b",
    r"\b(?:pensionsordning|pensionsselskab)\b.{0,40}\b(?:krav|fortrinsret|kun)\b",
)

NEGATED_RESTRICTION_PATTERNS = (
    r"\b(?:intet|ingen|ikke noget)\s+medlemskab\s+(?:er\s+)?(?:krav|kraeves)\b",
    r"\bmedlemskab\s+(?:er\s+)?ikke\s+(?:et\s+)?krav\b",
    r"\bmedlemskab\s+kraeves\s+ikke\b",
    r"\bingen\s+krav\s+om\s+medlemskab\b",
)

COMMERCIAL_PATTERNS = (
    r"\b(?:erhvervslejemal|erhvervslokale|kontorlokale|butikslokale|lagerlokale|restaurantlokale|kliniklokale)\b",
    r"\b(?:udlejes|anvendes|indrettet)\s+(?:til|som)\s+(?:erhverv|kontor|butik|lager|restaurant|klinik)\b",
    r"\b(?:erhverv|kontor|butik|lager|restaurant|klinik)\s+til\s+leje\b",
    r"\b(?:type|kategori)\s+(?:erhverv|kontor|butik|lager|restaurant|klinik)\b",
)

_DANISH_VARIANTS = (
    ("æ", "ae"),
    ("ø", "o"),
    ("å", "a"),
    ("Ã¦", "ae"),
    ("Ã¸", "o"),
    ("Ã¥", "a"),
    ("ÃƒÂ¦", "ae"),
    ("ÃƒÂ¸", "o"),
    ("ÃƒÂ¥", "a"),
)


def normalize_text(value):
    text = str(value or "").strip().lower()
    for source, replacement in _DANISH_VARIANTS:
        text = text.replace(source.lower(), replacement)
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", errors="ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def extract_amount(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    match = re.search(r"(-?)\s*(\d[\d.\s,]*)", str(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(2))
    if not digits:
        return None
    amount = int(digits)
    return -amount if match.group(1) else amount


def extract_postcode(value):
    match = re.search(r"\b(\d{4})\b", str(value or ""))
    return int(match.group(1)) if match else None


def is_preferred_postcode(postcode):
    if postcode is None:
        return False
    try:
        postcode = int(postcode)
    except (TypeError, ValueError):
        return False
    return 1000 <= postcode <= 2000 or postcode in PREFERRED_POSTCODES


def contains_restricted_eligibility(value, allow_cooperative_membership=False):
    text = normalize_text(value)
    if allow_cooperative_membership:
        text = re.sub(
            r"\b(?:kober(?:en)?\s+)?(?:bliver|skal\s+(?:blive|vaere))?\s*"
            r"medlem\s+af\s+(?:en\s+)?andelsforening(?:en)?\b",
            " ",
            text,
        )
    for pattern in NEGATED_RESTRICTION_PATTERNS:
        text = re.sub(pattern, " ", text)
    return any(term in text for term in RESTRICTED_TERMS) or any(
        re.search(pattern, text) for pattern in RESTRICTED_PATTERNS
    )


def contains_commercial_use(value):
    text = normalize_text(value)
    return any(re.search(pattern, text) for pattern in COMMERCIAL_PATTERNS)


def canonical_listing_key(address, transaction_type):
    return f"{normalize_text(transaction_type)}:{normalize_text(address)}"


def listing_matches_policy(listing):
    location = listing.get("location") or {}
    location_text = location.get("formatted", "") if isinstance(location, dict) else str(location)
    postcode = extract_postcode(f"{listing.get('name', '')} {location_text}")
    if not is_preferred_postcode(postcode):
        return False

    price = listing.get("price") or {}
    raw_amount = price.get("amount") if isinstance(price, dict) else price
    amount = extract_amount(raw_amount)
    if amount is None or amount <= 0:
        return False

    transaction_type = listing.get("transaction_type", "rent")
    eligibility_text = " ".join(
        str(listing.get(field) or "")
        for field in ("name", "description", "eligibility", "onlyFor", "requirements", "raw_text")
    )
    if contains_restricted_eligibility(
        eligibility_text,
        allow_cooperative_membership=transaction_type == "cooperative_sale",
    ) or contains_commercial_use(eligibility_text):
        return False

    default_limit = COOPERATIVE_SALE_MAX if transaction_type == "cooperative_sale" else GENERAL_RENT_MAX
    limit = int(listing.get("price_limit", default_limit))
    inclusive = bool(listing.get("price_limit_inclusive", transaction_type == "rent"))
    return amount <= limit if inclusive else amount < limit
