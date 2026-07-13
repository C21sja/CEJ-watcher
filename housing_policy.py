import math
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
    r"\b(?:kober(?:en)?\s+)?(?:bliver\s+)?medlem\s+af\s+(?:en\s+)?andels(?:bolig)?forening(?:en)?\b",
    r"\b(?:kraever|krav\s+om)\s+medlemskab\b",
    r"\b(?:pensionsordning|pensionsselskab)\b.{0,40}\b(?:krav|fortrinsret|kun)\b",
)

NEGATED_RESTRICTION_PATTERNS = (
    r"\b(?:intet|ingen|ikke noget)\s+medlemskab\s+(?:er\s+)?(?:krav|kraeves)\b",
    r"\bmedlemskab\s+(?:er\s+)?ikke\s+(?:et\s+)?krav\b",
    r"\bmedlemskab\s+kraeves\s+ikke\b",
    r"\bingen\s+krav\s+om\s+medlemskab\b",
    r"\b(?:der\s+er\s+)?intet\s+krav\s+om\s+medlemskab\b",
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
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() else value

    match = re.search(r"(-?)\s*(\d[\d.\s,]*)", str(value))
    if not match:
        return None
    number_text = match.group(2).strip()
    decimal_match = re.fullmatch(r"(\d[\d.\s]*),(\d{2})", number_text)
    if decimal_match:
        whole_digits = re.sub(r"\D", "", decimal_match.group(1))
        if not whole_digits:
            return None
        fraction = int(decimal_match.group(2))
        amount = int(whole_digits) + fraction / 100
        if fraction == 0:
            amount = int(whole_digits)
    else:
        digits = re.sub(r"\D", "", number_text)
        if not digits:
            return None
        amount = int(digits)
    return -amount if match.group(1) else amount


def extract_postcode(value):
    match = re.search(r"\b(\d{4})\b", str(value or ""))
    return int(match.group(1)) if match else None


def _extract_postcodes(value):
    return {int(code) for code in re.findall(r"\b(\d{4})\b", str(value or ""))}


def _extract_contextual_name_postcodes(value):
    text = str(value or "")
    normalized = normalize_text(text)
    postcodes = {
        int(code)
        for code in re.findall(
            r"\b(?:postnummer|post\s+nr|postcode)\s+(\d{4})\b",
            normalized,
        )
    }
    for segment in text.split(",")[1:]:
        match = re.match(
            r"^(\d{4})\s+(?:kobenhavn|koebenhavn|frederiksberg)\b",
            normalize_text(segment),
        )
        if match:
            postcodes.add(int(match.group(1)))
    return postcodes


def _select_listing_postcode(name, location_text):
    location_postcodes = _extract_postcodes(location_text)
    name_postcodes = _extract_contextual_name_postcodes(name)

    if len(location_postcodes) > 1:
        return None
    if location_postcodes:
        postcode = next(iter(location_postcodes))
        return None if name_postcodes - {postcode} else postcode
    return next(iter(name_postcodes)) if len(name_postcodes) == 1 else None


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
            r"medlem\s+af\s+(?:en\s+)?andels(?:bolig)?forening(?:en)?\b",
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


def deduplicate_listings(listings):
    selected = {}
    key_order = []

    for listing in listings:
        key = listing.get("canonical_key") or f"id:{listing.get('id')}"
        if key not in selected:
            selected[key] = listing
            key_order.append(key)
            continue
        if listing.get("source_priority", 100) < selected[key].get("source_priority", 100):
            selected[key] = listing

    return [selected[key] for key in key_order]


def listing_matches_policy(listing):
    location = listing.get("location") or {}
    location_text = location.get("formatted", "") if isinstance(location, dict) else str(location)
    postcode = _select_listing_postcode(listing.get("name", ""), location_text)
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
    if "price_limit" in listing:
        try:
            limit = extract_amount(listing.get("price_limit"))
        except (TypeError, ValueError, OverflowError):
            return False
        if limit is None or limit <= 0:
            return False
    else:
        limit = default_limit

    if "price_limit_inclusive" in listing:
        inclusive = listing.get("price_limit_inclusive")
        if not isinstance(inclusive, bool):
            return False
    else:
        inclusive = transaction_type == "rent"
    return amount <= limit if inclusive else amount < limit
