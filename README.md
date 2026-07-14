# CEJ / Mixed Rental Watcher

An automated Python script that monitors many Copenhagen rental- and
cooperative-sale-listing sources for new, status-changed, or newly-open
listings and posts a rich notification to a Discord channel. Despite the repo
name, it's a "Mixed Watcher" covering direct landlords, portals, a municipal
listing site, an aggregator with origin verification, and a small set of
project/application readiness monitors.

## Sources watched

### Original fast/slow sources

| Source | Area filter | Notes |
| --- | --- | --- |
| CEJ (`udlejning.cej.dk`) | Copenhagen K/V/N/Ø + Amager, ≤18,000 kr | Primary source; direct JSON API |
| Propstep | Same as CEJ | Paginated JSON API; also the transport for AKF (see below) |
| Sweet Homes | Same as CEJ | HTML scrape |
| City Apartment (`cityapartment.dk`) | **Amager, København K, Frederiksberg, Vesterbro, Østerbro only** | HTML scrape |
| Capital Bolig | København V + Frederiksberg | HTML scrape + one detail-page fetch per matching listing |
| Juli Living | København K | JSON API (slow: ~6-7s per fetch) |
| C.W. Obel | Islands Brygge | HTML scrape |

All sources also pass through a shared filter: max rent 18,000 kr/month and
exclusion of Rødovre/Hvidovre/Ballerup/Valby/Vanløse.

### Expanded no-waitlist / first-come-first-served sources

These were added to cover Copenhagen housing sources where speed of response
plays a large part and there is no membership-only waitlist. All content type,
cadence, eligibility, and price-range claims below were verified against a
12 July 2026 live, read-only probe (`docs/latest-source-scan.md` has the full
evidence — accepted/rejected counts, every accepted address/price/URL, and
every diagnostic).

| Source | Content type | Cadence | Accepted eligibility | Membership/age rule | Observed qualifying price range (12 Jul 2026) |
| --- | --- | --- | --- | --- | --- |
| Findbolig (Københavns Ejendomme only) | Rent | fast | Only exact `Københavns Ejendomme` **residence** records; rejects pension, project, property, and waitlist (`applicationType`/`rentModel` = `WaitingList`) records | None (municipal) | none active in this scan |
| Lejeboligmægleren | Rent | fast | `Ledig`/`Under opsigelse` cases; rejects student unit types and restricted tags | None observed | 9,200 – 18,000 kr |
| Norhjem | Rent | fast | Canonical live-overview listings only; rejects `Kun for studerende` cards | Student-only cards rejected | 8,850 – 17,400 kr |
| Taurus | Rent | ten_minute | `ledig`/`reserveret`/`udlejet` detail pages; rejects student/senior/membership-restricted cards | Restriction labels rejected when present | 14,480 – 17,974 kr |
| Brikk (cooperative sales) | Cooperative sale | ten_minute | Active andelsboliger strictly below `2,800,000 kr.`; ordinary cooperative-association buyer approval is disclosed but is not treated as a pension/age gate | Cooperative-association membership on purchase only (not a screening gate) | none active in this scan |
| Kobenhavn.dk | Discovery only | ten_minute | Strict `<15,000 kr.` rental cap / `<2,800,000 kr.` andel cap; **never alerts as a home without origin verification** | N/A (discovery-only) | none active in this scan (0 verified; all 8 current candidates are `manual_review` — see below) |
| AKF (via the existing Propstep response) | Rent | fast (no extra request) | Public, non-waitlist Propstep records only (`waitingLists: false`, `transactionStatus == 1`); AKF's numbered Waitly list is never queried | Restricted (`onlyFor`) records rejected | none active in this scan |
| RLE | Rent + readiness | ten_minute | Structured residential `vacancy` blocks only; commercial blocks rejected | None | none active in this scan (readiness: "No residential vacancies") |
| CPH Homes | Readiness only (never a confirmed-vacancy feed) | thirty_minute | N/A — availability language on a monitored page is inspection evidence, not a listing | N/A | N/A (see the current TLS caveat below) |
| Værnedamsvej (Den Franske Skole + DFE) | Readiness only (never a confirmed-vacancy feed) | ten_minute | N/A — a genuine new application link is an urgent alert; a routine project update is not | N/A | N/A |

The agreed postcode set is `1000-2000, 2100, 2150, 2200, 2300, 2400, 2450`
(København K/V, Frederiksberg, Ø, Nordhavn, N, S/Amager, NV, SV). Valby
(`2500`), Brønshøj (`2700`), Vanløse (`2720`), and Kastrup (`2770`) are
explicitly excluded, along with Rødovre/Hvidovre/Ballerup.

**Known live-data caveat (12 July 2026):** CPH Homes currently fails every
fetch with a TLS certificate error (`cphhomes.dk`'s certificate does not cover
its own hostname — see `docs/latest-source-scan.md`). This is a site-side
infrastructure problem, not a parsing bug; the watcher does not, and will not,
disable certificate verification to work around it (that is explicitly out of
scope). The source stays registered and will start working again automatically
once the site's certificate is fixed, without any code change, because each
source's failure is isolated from the others.

**Kobenhavn.dk manual-review caveat:** the live scan found akutbolig.dk (the
sole rental-section origin) has migrated to a client-rendered app with no
server-rendered listing markup on its category pages, so the previously
workable "membership in a rendered inventory page" verifier is not currently
registered for it. The Andelsbolig (cooperative-sale) section links out to
eleven different broker platforms, none of which has a captured/tested
extractor yet. Every current Kobenhavn.dk candidate therefore produces a
`manual_review` diagnostic and a single, non-mention "needs verifier review"
Discord event instead of ever being presented as a confirmed home — this is
the intended fail-closed behavior, not a bug.

### Baseline digest and ongoing alert behavior

- A newly added source's first successful fetch produces one compact digest
  chunk per source (grouped together when they fit under Discord's message
  size limit), with a single `@everyone`/mention across the whole digest
  batch — not once per source.
- Reserved/unavailable records are seeded into state silently (no baseline
  line, no alert) but are not forgotten: a later transition to an active
  status still alerts normally.
- If a source's baseline digest chunk fails to send, that source is not
  marked seeded; its next successful fetch produces a fresh, source-specific
  catch-up digest instead of a burst of individual "new listing" alerts.
- Everything after a source's baseline is seeded uses the existing individual
  per-listing alert (new listing found / status changed) or, for readiness
  sources (RLE inspection cases, CPH Homes, Værnedamsvej), a labelled
  readiness alert. Only a genuine Værnedamsvej application opening or RLE/CPH
  Homes urgent inspection mentions `@everyone`; routine project updates and
  inspection notices do not ping.

### Manual-only sources (not scraped)

Two sources have no scrapeable live cards and are handled as manual,
one-time interest-list emails instead of trackers — see
`docs/manual-contact-emails.md` for the ready-to-send Danish drafts (never
sent automatically):

- **ØENS Ejendomsadministration** (`lejer@oadv.dk`) — its interest list is
  explicitly not a numbered waitlist, but publishes no live cards. Entries
  are deleted after 12 months; set a manual renewal reminder 11 months after
  sending.
- **Ejendomskontoret** (`udlejning@ejendomskontoret.dk`) — primarily
  corporate/embassy/international-organisation contracts; no live feed to
  scrape.

## Why adaptive polling

The watcher used to wake up once every 30 minutes, poll every source once, and
exit. That is why a "New Apartment Alert" could already show `Reserved` --
the listing had gone `Available -> Reserved` inside that 30-minute blind
window. This was fixed with a data-driven approach (see `watcher.py` for the
full writeup and citations):

1. **Real publish/status-change timestamps**: CEJ's own API exposes
   `lastPublishedDate`, which updates on every publish *and* every status
   transition. A live snapshot showed events concentrated almost entirely on
   weekday business hours in Copenhagen local time (~62% between 08:00-13:00,
   ~24% between 13:00-17:00, none on weekends) -- consistent with the two real
   Discord detections on record (Thu 16:40, Fri 15:52 CPH) and the weaker
   signal from `seen_ids.json` commit history (peak ~12:00-15:00 CPH, almost
   all Mon-Fri).
2. **No CDN in front of CEJ's API**: repeated requests a few seconds apart
   returned identical bodies with no `Age`/`ETag`/`X-Cache` headers and
   consistent ~0.8-1.1s origin latency (`Via: fly.io`), so there's no cache TTL
   to synchronize against the way there is for some other feeds. Polling
   cadence is therefore a plain fixed interval per tier, not cache-synced.
3. CEJ has rate-limited this watcher before, so the fast tier is deliberately
   conservative rather than as aggressive as it could be against a CDN-backed
   feed.

### Polling schedule (Copenhagen local time, DST-aware, zero dependencies)

| Tier | When (Copenhagen local) | Default interval |
| --- | --- | --- |
| `HOT` | Weekdays 08:00-13:00 (observed peak) | 45s |
| `WARM` | Weekdays 07:00-08:00 and 13:00-18:00 (afternoon tail + ramp-up) | 90s |
| `COOL` | Weekdays 18:00-22:00; weekends 09:00-20:00 | 240s |
| `COLD` | Overnight and the rest of the weekend | 900s |

Every source is registered with a cadence in `watcher.make_source_registry()`
and scheduled independently by `fetch_due_sources()`, rather than two
hard-coded name groups:

- **`fast`** (this tier schedule): CEJ, Propstep, Sweet Homes, City Apartment,
  Findbolig, Lejeboligmægleren, and Norhjem -- cheap, single/small-request
  fetches, polled every cycle. AKF is not a separate fetch: it's classified
  inside the existing Propstep response.
- **`ten_minute`** (`WATCHER_SLOW_SOURCE_INTERVAL_SECONDS`, default 600s):
  Capital Bolig, Juli Living, C.W. Obel, Taurus, Brikk, Kobenhavn.dk, RLE, and
  Værnedamsvej -- either slow/heavier to fetch or not worth polling as
  aggressively as CEJ.
- **`thirty_minute`** (`WATCHER_READINESS_SOURCE_INTERVAL_SECONDS`, default
  1800s, floored at 300s): CPH Homes -- a static readiness monitor that is
  fragile to over-poll.

One source failing (`fetch_due_sources()` catches and logs per-source
exceptions) never suppresses any other source's fetch that cycle.

## Environment Variables / Repository Secrets

- `DISCORD_WEBHOOK_URL`: The webhook URL for the Discord channel where alerts are sent.
- `DISCORD_MENTION_EVERYONE`: `true`/`false` (default `true`) -- ping `@everyone`.
- `DISCORD_MENTION` / `DISCORD_MENTION_USER_ID`: Used instead of `@everyone` when `DISCORD_MENTION_EVERYONE=false`.
- `CEJ_MAX_ATTEMPTS` / `CEJ_RETRY_BASE_SECONDS`: Retry/backoff tuning for CEJ 429/503 responses (defaults: 5 attempts, 10s base).

### Polling configuration (all optional)

- `WATCHER_ADAPTIVE_POLLING`: Set to `false` to disable the tiered schedule and poll every source at a flat `WATCHER_SLEEP_SECONDS` interval instead.
- `WATCHER_SLEEP_SECONDS`: Fixed interval used only when adaptive polling is disabled, or by the legacy single-shot mode below (default 60).
- `WATCHER_POLL_HOT_SECONDS` / `WATCHER_POLL_WARM_SECONDS` / `WATCHER_POLL_COOL_SECONDS` / `WATCHER_POLL_COLD_SECONDS`: Override any tier's fast-source interval (floored at 10s to stay polite to CEJ's origin).
- `WATCHER_SLOW_SOURCE_INTERVAL_SECONDS`: Interval for every `ten_minute`-cadence source: Capital Bolig, Juli Living, C.W. Obel, Taurus, Brikk, Kobenhavn.dk, RLE, Værnedamsvej (default 600 = 10 min, floored at 60s).
- `WATCHER_READINESS_SOURCE_INTERVAL_SECONDS`: Interval for `thirty_minute`-cadence readiness sources (currently only CPH Homes) (default 1800 = 30 min, floored at 300s).
- `WATCHER_MAX_RUNTIME_SECONDS`: How long a single continuous run stays alive before exiting cleanly to let the next scheduled job take over (default 4200 = 70min; the GitHub workflow sets this explicitly to match its cron interval).
- `WATCHER_RUNS`: Legacy mode. If set to a positive number, the watcher performs exactly that many polls of *every* source at a flat `WATCHER_SLEEP_SECONDS` interval, then exits, instead of the default continuous adaptive loop. Leave unset (or `0`) to use adaptive polling.

### State file metadata (`seen_ids.json`)

Alongside plain listing-status entries (unchanged), the state file uses these
reserved, namespaced keys — never treated as listings:

```text
__meta__:baseline:<source>            per-source baseline-digest completion
__meta__:baseline-mention             whether the one baseline mention was sent
__meta__:baseline-chunk:<sha256>      per-chunk delivery dedup for digest retries
__meta__:readiness:<event-id>         last-sent signature/registration-closed/signals for a readiness source
__meta__:listing:<canonical-key-sha256>  cross-source canonical listing state (falls back to the raw ID when a listing has no canonical_key)
```

## Running Locally

```bash
python watcher.py
```

*Note: On the very first run, the script caches all current listings in
`seen_ids.json` without notifying Discord, to avoid spamming the channel on
initial setup.*

## Running via GitHub Actions

`.github/workflows/watcher.yml` runs the watcher continuously:

1. Add the environment variables above as repository secrets/variables.
2. The workflow triggers **hourly** (`0 * * * *`). Each job runs for up to ~70
   minutes (`WATCHER_MAX_RUNTIME_SECONDS=4200`) -- longer than the 60-minute
   trigger interval -- so a fresh run is always queued and, serialized by a
   job-scoped `concurrency` group, takes over the instant the previous job
   exits. This gives continuous coverage with no blind gaps between runs and
   tolerates GitHub's occasional late/dropped scheduled triggers.
3. `push`/`pull_request` events only run the fast `verify` job (syntax check +
   unit tests); the concurrency group that serializes the long poll loop is
   scoped to the scheduled `run-watcher` job only, so CI feedback on PRs isn't
   blocked behind an in-progress poll run.
