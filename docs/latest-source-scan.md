# Latest source scan

**Captured:** 12 July 2026 (probe run 2026-07-14T10:34Z), read-only, `DISCORD_WEBHOOK_URL` unset so no Discord delivery occurred.

**Command used** (see Task 15 / Step 5 of the implementation plan):

```powershell
$env:DISCORD_WEBHOOK_URL = ""
python -c "import time, watcher; registry=watcher.make_source_registry(); snapshots,ok=watcher.fetch_due_sources(registry,time.monotonic(),{}); ..."
```

This is a one-time, read-only evidence snapshot. It is not re-run
automatically; re-run it manually before relying on the price ranges below
for anything time-sensitive.

## Successful sources (15)

Brikk, C.W. Obel, CEJ, Capital Bolig, City Apartment, Findbolig, Juli Living,
Kobenhavn.dk, Lejeboligmægleren, Norhjem, Propstep, RLE, Sweet Homes, Taurus,
Værnedamsvej.

## Failed sources (1)

**CPH Homes** — every pinned page fetch failed with:

```text
<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, certificate is not valid for 'cphhomes.dk'. (_ssl.c:992)>
```

Investigation: `cphhomes.dk`'s TLS certificate (issued by Sectigo, subject
containing `dandomain.dk`) does not cover `cphhomes.dk` or `www.cphhomes.dk`
at all — this is a site-side certificate/hosting misconfiguration, not a
watcher parsing bug. The watcher does not disable certificate verification to
work around this (explicitly out of scope). This failure is correctly
isolated: it did not prevent any other source, including the other
`thirty_minute`-cadence source's siblings, from fetching successfully. No
readiness event, listing, or price range is recorded for CPH Homes in this
scan; a failure is not equivalent to zero matches, and it is not silently
reinterpreted as "monitoring ready."

## Accepted listings by source

### Newly added sources (this feature)

**Findbolig** — accepted 0. Company UUID `73d07df9-6e80-4b79-da2d-08dbb5297ffe`
(Københavns Ejendomme) currently has zero advertised residences matching the
municipal-owner + non-waitlist filter. Verified via a live, sanitized capture
of the real `/api/search` contract (see `tests/fixtures/findbolig/`); the
filter mechanism itself was proven to work by re-querying with a different,
known-active company ID and getting a non-zero result. **Price range: none
active in this scan.**

**Lejeboligmægleren** — accepted 6.

| Address | Price (kr/month) | URL |
| --- | --- | --- |
| Else Alfelts Vej 60E | 15,700 | https://lejeboligmaegleren.dk/cases/19500/ |
| Else Alfelts Vej 66D | 18,000 | https://lejeboligmaegleren.dk/cases/18459/ |
| Else Alfelts Vej 64C | 15,400 | https://lejeboligmaegleren.dk/cases/18685/ |
| Poppelstykket 6 | 9,200 | https://lejeboligmaegleren.dk/cases/17483/ |
| Else Alfelts Vej 60E | 15,500 | https://lejeboligmaegleren.dk/cases/19733/ |
| Poppelstykket 8 | 9,600 | https://lejeboligmaegleren.dk/cases/17579/ |

**Price range: 9,200 – 18,000 kr.**

**Norhjem** — accepted 4.

| Address | Price (kr/month) | URL |
| --- | --- | --- |
| Nørrebrogade 32 C, 3. tv., 2200 København N | 17,400 | https://norhjem.dk/ejendomme/noerrebrogade/noerrebrogade-32-c-3-tv/ |
| Teglholmsgade 36, 6. Dør3, 2450 København SV | 8,850 | https://norhjem.dk/ejendomme/teglhuset/teglholmsgade-36-6-doer3/ |
| Strynøgade 5, 3. 03, 2100 København Ø | 9,150 | https://norhjem.dk/ejendomme/strynoegade/strynoegade-5-3-03/ |
| Strynøgade 5, 1. 06, 2100 København Ø | 9,900 | https://norhjem.dk/ejendomme/strynoegade/strynoegade-5-1-06/ |

**Price range: 8,850 – 17,400 kr.**

**Taurus** — accepted 5. (This scan's probe also surfaced and fixed a real
defect: Taurus's live detail-page markup uses `<li><strong>Label</strong>:
<span>value</span></li>`, not the originally assumed `<p><strong>Label:</strong>
value</p>`, and never includes a separate "By" (city) label at all — only
"Postnummer". Both are now handled; see `housing_sources/landlords.py`.)

| Address | Price (kr/month) | URL |
| --- | --- | --- |
| Nørrebrogade 190, 2200 | 17,500 | https://www.taurus.dk/boligudlejning/lejebolig?id=26350 |
| Borgmester Christiansens Gade 47, 2450 | 14,480 | https://www.taurus.dk/boligudlejning/lejebolig?id=22588 |
| Dirch Passers Allé 20, 2000 | 16,657 | https://www.taurus.dk/boligudlejning/lejebolig?id=22404 |
| Fanny Jensens Plads 7, 2450 | 17,713 | https://www.taurus.dk/boligudlejning/lejebolig?id=22238 |
| Andrea Brochmanns Gade 11, 2450 | 17,974 | https://www.taurus.dk/boligudlejning/lejebolig?id=15512 |

**Price range: 14,480 – 17,974 kr.**

**Brikk** — accepted 0 (zero active andelsboliger below 2,800,000 kr. in the
agreed areas at capture time). **Price range: none active in this scan.**

**AKF (via Propstep)** — accepted 0 in this scan (classified inside the
existing Propstep response; no matching public, non-waitlist AKF record was
present at capture time). **Price range: none active in this scan.**

**RLE** — accepted 0 listings; one readiness event: *"No residential
vacancies"* (`https://rle.dk/ledige-ejendomme`, not urgent). **Price range:
none active in this scan.**

**Værnedamsvej** — accepted 0 listings (readiness monitor, never a
confirmed-vacancy feed); one readiness event: *"Seneste nyt om byggeriet —
20. maj 2026"* (`https://denfranskeskolevaernedamsvej.dk/status-pa-projektet/`,
not urgent — a routine project update, not an application opening).

**CPH Homes** — see "Failed sources" above; no range recorded.

### Kobenhavn.dk — discovery feed, origin-host manifest

Kobenhavn.dk accepted 0 confirmed listings and produced 8 candidate
diagnostics plus 1 non-mention `manual_review` inspection event
(`"Kobenhavn.dk has an origin that needs verifier review"`,
`https://www.kobenhavn.dk/bolig`, not urgent). The captured origin-host
manifest lives at `tests/fixtures/kobenhavn_dk/origin_manifest.json`.

| Origin host | Verifier status | Reason |
| --- | --- | --- |
| akutbolig.dk | **Not verified** | Live capture found it has migrated to a client-rendered app: its category pages (e.g. `https://www.akutbolig.dk/koebenhavn-v`) no longer contain server-rendered `/vis/{id}` markup, and the direct `/vis/{id}` URL now 404s. The "membership in a rendered inventory page" extractor (`_akutbolig_record`) is kept in the codebase (and covered by a monkey-patched unit test) for potential future reuse, but is deliberately not registered against a host that cannot currently be verified this way. |
| brikk.dk | Not verified | Sale rows link to `https://www.brikk.dk/redirect?caseNumber=...`; a live probe of that redirect endpoint did not return a response within a reasonable timeout during this scan. Not registered pending a reliable capture. |
| home.dk | Not verified | No captured/tested extractor yet. |
| nybolig.dk | Not verified | No captured/tested extractor yet. |
| realmaeglerne.dk | Not verified | No captured/tested extractor yet. |
| soeboe-ejendomme.dk | Not verified | No captured/tested extractor yet. |
| edc.dk | Not verified | No captured/tested extractor yet. |
| eltoftnielsen.dk | Not verified | No captured/tested extractor yet. |
| danbolig.dk | Not verified | No captured/tested extractor yet. |
| unikboligsalg.dk | Not verified | No captured/tested extractor yet. |
| estate.dk | Not verified | No captured/tested extractor yet. |
| adamschnack.dk | Not verified | No captured/tested extractor yet. |

Every diagnostic from this scan:

| Outcome | Candidate | Reason | Origin URL |
| --- | --- | --- | --- |
| manual_review | rent:akutbolig.dk:486255 | new origin host has no captured verifier | https://www.akutbolig.dk/vis/486255#... |
| manual_review | rent:akutbolig.dk:501986 | new origin host has no captured verifier | https://www.akutbolig.dk/vis/501986#... |
| manual_review | rent:akutbolig.dk:313162 | new origin host has no captured verifier | https://www.akutbolig.dk/vis/313162#... |
| manual_review | rent:akutbolig.dk:649115 | new origin host has no captured verifier | https://www.akutbolig.dk/vis/649115#... |
| manual_review | cooperative_sale:home.dk:1020001269 | new origin host has no captured verifier | https://home.dk/sag/1020001269 |
| manual_review | cooperative_sale:nybolig.dk:property.action | new origin host has no captured verifier | https://www.nybolig.dk/maegler/pages/property-presentation/property.action?caseno=AT000001&shopno=102990 |
| manual_review | cooperative_sale:brikk.dk:redirect | new origin host has no captured verifier | https://www.brikk.dk/redirect?caseNumber=192SK-242290 |
| manual_review | cooperative_sale:unikboligsalg.dk:CW415 | new origin host has no captured verifier | https://unikboligsalg.dk/koeb/sag/CW415 |

None of these was, or ever will be, presented as an active home while
unverified. This is intentional fail-closed behavior, not a coverage gap in
the sense of a bug — it is documented here specifically because the
implementation plan's release gate requires every current manifest host to
be visibly accounted for rather than silently treated as "zero matches."

### Existing sources (unchanged by this feature, included for completeness)

CEJ accepted 28 (price range 5,905 – 32,780 kr — several results are outside
Copenhagen proper, e.g. Ballerup/Lyngby/Rødovre, because CEJ's own
keyword-based location filter is broader than the postcode-based policy used
by the new sources); Propstep accepted 93 (11,400 – 18,000 kr); Sweet Homes
accepted 17 (several `Unknown` prices; parsed prices ranged 15,400 – 17,495
kr); Capital Bolig accepted 3 (950 – 22,200 kr, including parking/garage
rentals); Juli Living accepted 1 (23,900 kr); City Apartment and C.W. Obel
accepted 0 in this scan.

## Acceptance criteria verified in this scan

- Every reachable source returned a contract-valid `SourceSnapshot` (no
  parser exception masquerading as an empty feed).
- No listing outside the agreed areas or price limits was accepted by any of
  the newly added sources.
- No stale Kobenhavn.dk row, sold Brikk card, student-only Norhjem/Taurus
  card, or AKF Waitly record reached the accepted-listings list.
- CPH Homes's failure was investigated (TLS certificate issue on the site's
  side) and is recorded as a failure, not a false "zero vacancies" or "ready"
  state.
