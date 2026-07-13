# Expanded Copenhagen Housing Trackers Design

**Date:** 2026-07-13
**Status:** Conversational design approved; written specification awaiting user review

## Context

The repository is a zero-dependency Python housing watcher that polls several Copenhagen rental sources, normalizes their listings, filters them, persists status in `seen_ids.json`, and sends Discord webhook alerts for new listings and status changes. It currently has a large `watcher.py`, hard-coded fast and slow source groups, and source-specific location rules that do not consistently match the user's final preferred areas.

This change adds the explicitly selected rental, cooperative-sale, and project-readiness sources. It also introduces one shared geographic policy and a compact first-run Discord digest so all qualifying current listings reach Discord without producing an individual-alert flood.

## Goals

1. Track every verified qualifying listing from the selected sources in the agreed central Copenhagen areas.
2. Track the Værnedamsvej project closely enough to alert when applications or registration become possible.
3. Send all current matches from newly added sources to Discord in a compact baseline digest, then send individual alerts for later listings and status changes.
4. Prevent false alerts from stale aggregators, sold cooperative homes, waitlist records, student-only homes, commercial premises, and ambiguous page changes.
5. Preserve the existing zero-dependency runtime and keep failures in one source from stopping the other sources.

## Scope

### New structured listing coverage

- Findbolig: only residences owned by Københavns Ejendomme.
- Brikk: active cooperative homes below the sale-price ceiling.
- Kobenhavn.dk: discovery candidates only after the originating site confirms they are active.
- Taurus.
- Lejeboligmægleren.
- Norhjem.
- AKF direct public listings, classified inside the existing Propstep feed.
- RLE residential vacancies.

### New readiness coverage

- CPH Homes' relevant portfolio pages and newly published posts.
- Den Franske Skole/Værnedamsvej project status page.
- DFE's corresponding Værnedamsvej project page, because it currently carries the explicit registration-closed statement.

### Existing coverage

Existing CEJ, Propstep, Sweet Homes, City Apartment, Capital Bolig, Juli Living, and C.W. Obel integrations remain active. Every arbitrary listing must pass the same agreed-postcode policy; a source may use a fixed-location exception only when it represents one known project address. Existing seen listings will not be replayed in the new baseline digest.

### Manual contact handoff

- ØENS is not scraped as a vacancy source because it publishes no live cards. The user received a Danish email template for `lejer@oadv.dk` containing every required interest-list field. ØENS deletes entries after 12 months, so renewal is a manual follow-up 11 months after the email is sent.
- Ejendomskontoret is not added as a tracker in this change. The user received a Danish email template for `udlejning@ejendomskontoret.dk` that explicitly distinguishes a private applicant from an employer-backed applicant and does not imply corporate eligibility.

### Out of scope

- Automatically sending either contact email.
- Logging in to private accounts, paid lists, Waitly, pension portals, or membership-only pages.
- Circumventing CAPTCHAs, bot protection, or TLS validation.
- Creating a listing from an unverified aggregator row or an unstructured page revision.
- Tracking the other researched landlords and projects that the user did not select.

## Shared Eligibility Policy

### Areas

A normal listing must have one of these postcodes:

- `1000-1499`: København K, including Christianshavn.
- `1500-1799`: København V/Vesterbro.
- `1800-2000`: Frederiksberg/Frederiksberg C.
- `2100`: København Ø.
- `2150`: Nordhavn.
- `2200`: København N/Nørrebro.
- `2300`: København S/central Amager.
- `2400`: København NV.
- `2450`: København SV.

Postcodes `2500`, `2700`, `2720`, and `2770` are explicitly excluded, as are Rødovre, Hvidovre, Ballerup, and other outer municipalities. Arbitrary listing sources must provide a postcode; a text-only location fallback is allowed only for a fixed project whose address is known in advance, such as Værnedamsvej.

### Prices and transaction types

- Ordinary rentals: monthly base rent at or below `18,000 kr.`
- Kobenhavn.dk rentals: strictly below `15,000 kr.`
- Brikk and Kobenhavn.dk cooperative sales: strictly below `2,800,000 kr.`
- Unknown, zero, negative, or malformed prices are rejected for normal listings.
- Readiness events do not require a price because they are not presented as available homes.

### Eligibility guards

- Reject records explicitly restricted to students, youths, seniors, pension members, or another membership group unless the user has explicitly selected that flow.
- AKF is the one deliberate queue-related exception. Only its public, non-waitlist Propstep records are accepted; its numbered Waitly flow is not queried.
- Reject commercial premises and records described as offices, shops, clinics, restaurants, warehouses, or similar uses.
- Reserved or rented records may be retained only when needed to detect a later transition to available. They are not included as active baseline matches.

## Architecture

### Source adapters

New source logic will live outside the already large `watcher.py`:

```text
housing_policy.py
housing_sources/
    __init__.py
    findbolig.py
    brikk.py
    kobenhavn_dk.py
    landlords.py
    readiness.py
```

`housing_policy.py` owns postcode, price, eligibility, text normalization, and canonical-address rules. The source modules own only transport contracts and source-specific parsing. They receive injectable HTTP helpers so parser tests use local fixtures instead of the network.

Each structured adapter returns the repository's existing listing dictionary with these optional additions:

- `transaction_type`: `rent` or `cooperative_sale`.
- `canonical_key`: normalized full address plus transaction type when cross-source deduplication is needed.
- `price_period`: `month` for rent or `total` for a sale.

Readiness adapters return a separate event dictionary with a stable ID, source, headline, status signature, URL, urgency, and explanatory text. Readiness events bypass rental-price filtering and use their own notification formatter.

### Source registry and scheduling

The hard-coded fetch groups will become a small source registry. Each source definition supplies its fetch function, cadence, and error label. This permits three cadences without duplicating orchestration logic:

- Adaptive fast tier: existing fast sources, Findbolig, Lejeboligmægleren, and Norhjem.
- Existing Propstep fast tier: AKF is classified from this response and adds no request.
- Ten-minute tier: Taurus, Brikk, Kobenhavn.dk, RLE, and both Værnedamsvej pages.
- Thirty-minute tier: CPH Homes, whose pages are static and technically fragile.

The scheduler tracks each source's next due time. A failure advances only that source's next attempt and never aborts unrelated sources.

## Source Contracts and Guards

### Findbolig municipal rentals

- Read the public search configuration and submit the same-origin search request used by the frontend.
- Filter by exact company UUID `73d07df9-6e80-4b79-da2d-08dbb5297ffe`, named `Københavns Ejendomme`.
- Accept only residence results with direct `/residence/{id}` links.
- Reject property, project, pension, and waiting-list records even if their location is Copenhagen.
- Revalidate the owner identifier, postcode, and rent locally before returning a listing.

### Brikk cooperative sales

- Fetch the user-supplied Copenhagen-andels search and follow `p_page` pagination until an empty page or the final page, subject to a defensive page limit.
- Require cooperative-home type, an allowed postcode, and a positive price below `2,800,000 kr.`
- Reject `SOLGT`, accepted-offer language, and zero-price cards.
- Use the full normalized address and transaction type as the canonical key because slugs can change or gain numeric suffixes.

### Kobenhavn.dk discovery feed

- Parse the rental and cooperative-home sections from the server-rendered page.
- Reject malformed rows, missing addresses, invalid dimensions, and threshold failures.
- Treat every remaining row as a candidate, not an active listing.
- Alert only after the linked originating source gives positive evidence that the same full unit and transaction type are currently active. Positive evidence is either membership in the origin's current inventory or an explicit structured/visible active status on the origin record. When the candidate lacks floor/door data, its rooms, area, and price must also match the origin closely enough to distinguish the unit. The origin's current price replaces the aggregator price and must independently pass the configured threshold. An HTTP 200 response or absence of the word `sold` is not sufficient.
- Every origin host present in the implementation-time scan is a release-gated verifier target. Unsupported or ambiguous records on known hosts are logged once by candidate ID and suppressed. A genuinely new host appearing after release is not fetched; it produces one non-mention manual-review inspection event so the coverage gap is visible without presenting the candidate as a home.

### Taurus

- Parse the single server-rendered vacancy page and its stable `?id=` cards.
- Read the card's location, price, rooms, area, and active status from structured attributes and detail data.
- Apply the shared area and `18,000 kr.` filters.
- Use the Taurus record ID rather than an address-only ID.

### Lejeboligmægleren

- Use `POST /Umbraco/Api/Case/Search` with `PageIndex` and `PageSize`.
- Stop on an empty result page; do not trust the currently unreliable `HasMorePages` flag by itself.
- Accept `Ledig` and `Under opsigelse` as actionable states.
- Retain but do not baseline records marked `Kontrakt under udarbejdelse`; a later transition back to an actionable state must alert.
- Use the stable case ID and direct `/cases/{id}/` link.

### Norhjem

- Treat the canonical live vacancy overview as the source of truth.
- Do not accept an orphan detail page that is absent from the overview.
- Reject cards labelled `Kun for studerende` and any other explicit eligibility restriction.
- Require a live/available marker, allowed postcode, and rent at or below `18,000 kr.`

### AKF via Propstep

- Do not add a second network request.
- In the existing Propstep response, classify company ID `5db6d00f4e5146201ae72ada` as `AKF via Propstep`.
- Preserve `propstep:{property.id}` as the ID so an item cannot alert twice.
- Require `waitingLists: false`, `transactionStatus == 1`, an allowed postcode, and rent at or below `18,000 kr.`
- Reject restrictions exposed through `propertyDetails.onlyFor` or equivalent description text.

### RLE

- Read the public Sanity document backing the official vacancy page.
- Compare normalized vacancy content rather than `_updatedAt` alone.
- Parse future cards only when they contain residential language and an allowed postcode and do not contain commercial-use terms.
- Prefer the Sanity block `_key`; fall back to normalized full address.
- If the current no-vacancy sentence disappears but the replacement cannot be parsed, create one readiness inspection event instead of inventing a listing.

### CPH Homes

- Monitor the relevant Holmen, Sydhavnen, Ørestad, Islands Brygge, and Engholmene portfolio pages plus newly published posts.
- Do not consume the site's plain-HTTP WordPress REST fallback. Fetch the pinned canonical pages over HTTPS and discover new posts only from scoped `<article>` links on the pinned HTTPS homepage; follow only same-host HTTPS post URLs with a defensive limit.
- Baseline normalized main content, excluding navigation, footer, timestamps unrelated to content, scripts, and styling.
- A change containing availability language, rent, a kroner amount, a preferred postcode, or a new outbound application link creates a readiness event. A fixture-backed exact external application host may contribute its full HTTPS URL; a previously unseen external host contributes only a host-review signal while the event continues to link to the trusted CPH Homes page.
- An otherwise material but unparseable change creates one `CPH Homes changed - inspect now` event keyed by page ID and revision. It is never presented as a confirmed home.

### Værnedamsvej application readiness

- Monitor both official Danish pages.
- Persist the newest project-update tuple: title, displayed date, and normalized first paragraph.
- An ordinary change to that tuple creates a project-status event.
- Create an urgent `APPLICATION OPENING` event when a new form, button, or link appears with strong positive terms such as `skriv dig op`, `opskrivning`, `interesseliste`, `tilmelding`, `ansøg`, `ledige boliger`, `boliger til leje`, or `book fremvisning`. Treat the generic label `se boliger` as inspection evidence unless the same scoped page also states that applications or homes are currently open.
- The disappearance or replacement of DFE's current registration-not-possible statement is also urgent.
- Negation-aware matching must suppress phrases such as `ikke skrive sig op`, `ikke åbent`, and `ikke muligt`.
- Ignore generic building-permit, hotel, Instagram, footer, privacy, and English-duplicate text.

## Deduplication

Source-native stable IDs remain the default. Cross-source deduplication uses `canonical_key`, built from the normalized complete address, floor/door information, postcode, and transaction type.

When duplicate candidates appear in one batch, the preferred record order is:

1. A positively verified originating listing.
2. A direct landlord or manager listing.
3. Brikk.
4. Kobenhavn.dk, which can never win without origin verification.

AKF always retains the existing Propstep ID. A missing or incomplete address prevents a normal listing alert rather than risking a collision.

## Discord Behavior

### Baseline digest

The baseline applies only to sources introduced by this feature.

1. Each new source has its own baseline marker in `seen_ids.json`.
2. At the end of the first successful fetch cycle, all active qualifying listings from successfully fetched new sources are grouped into a compact digest.
3. The digest includes a count per source, every active listing as a compact address/price/link line, and one-line status entries for readiness sources with zero listings.
4. The first payload contains the configured mention. Additional chunks are sent only when Discord's conservative size limit requires them and contain no mention.
5. Listings and source baseline markers are persisted only after every required digest chunk for that source succeeds.
6. A source that failed during the first cycle remains unseeded. Its first later success produces a compact source-specific catch-up digest, not a burst of individual alerts.

Existing sources and existing `seen_ids.json` entries are not replayed.

### Ongoing alerts

- A new listing or a status transition produces the existing individual listing alert.
- A normal project/page change produces a clearly labelled readiness alert.
- A positively detected Værnedamsvej application opening produces an urgent alert with the configured mention and direct link.
- Unverified Kobenhavn.dk records on known hosts produce logs, not Discord alerts. A new future origin host produces one non-mention inspection alert about the missing verifier; it never presents the candidate as an active home.
- CPH Homes inspection notices explicitly say that no confirmed vacancy was parsed.

## State Compatibility

`seen_ids.json` remains a JSON object and existing listing-status strings remain valid. Reserved, namespaced metadata keys store:

- Per-source baseline completion.
- Readiness signatures.
- One-time ambiguous-candidate or inspection-event suppression.

Loading old state requires no migration. Metadata keys are never treated as listings. If Discord delivery fails, neither the affected item status nor its baseline marker is advanced.

## Error Handling

- Every source fetch is isolated with a source-labelled error.
- Pagination has explicit maximum-page and maximum-item guards.
- Invalid JSON, unexpected HTML, missing required fields, and impossible prices suppress that record and emit a diagnostic.
- A source returning zero after previously returning listings is accepted only when the response structure itself is valid; a parser failure must not masquerade as a genuine empty feed.
- Origin verification and readiness classification fail closed: ambiguity produces no confirmed-listing alert.
- Existing Discord retry and rate-limit handling remains in use for individual alerts and is extended to digest chunks.

## Testing Strategy

Implementation will be test-driven with local HTML/JSON fixtures and mocked HTTP. Tests cover:

1. Every allowed postcode boundary and every explicit exclusion.
2. Rental and sale threshold inclusivity/strictness.
3. Exact Findbolig municipal-owner acceptance and private/pension/waitlist rejection.
4. Brikk pagination, sold markers, zero prices, and canonical identities.
5. Kobenhavn.dk parsing plus positive and negative origin-verification cases.
6. Taurus, Lejeboligmægleren, and Norhjem parsing, statuses, pagination, orphan pages, and restricted homes.
7. AKF classification without a second fetch or duplicate ID.
8. RLE residential/commercial separation and ambiguous content changes.
9. CPH Homes normalized revisions and inspection-only alerts.
10. Værnedamsvej positive, negated, and unrelated keyword cases.
11. Cross-source deduplication precedence.
12. Rental-versus-sale price formatting.
13. Per-source scheduling and failure isolation.
14. Baseline digest chunking, a single mention, catch-up behavior, and seed-only-after-success semantics.
15. Backward compatibility with the existing state file and current source tests.

## Verification and Acceptance Criteria

- `python -m unittest test_watcher -v` and all new tests pass.
- `python -m py_compile` succeeds for every Python module.
- A dry run against captured/live read-only responses lists expected accepted and rejected counts without posting to Discord.
- A mocked Discord integration run proves that the baseline is compact, mentions once, and seeds only after successful delivery.
- The existing sources continue to fetch independently.
- No listing outside the agreed areas or price limits reaches Discord.
- No stale Kobenhavn.dk row, sold Brikk card, student-only Norhjem card, Waitly record, or commercial RLE block reaches Discord as a home.
- A simulated Værnedamsvej application link produces one urgent direct-link alert, while a negated statement produces none.
