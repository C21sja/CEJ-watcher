# CEJ / Mixed Rental Watcher

An automated Python script that monitors several Copenhagen rental-listing
sources for new or status-changed apartments and posts a rich notification to
a Discord channel. Despite the repo name, it's a "Mixed Watcher": alongside
CEJ (Udlejning) it also polls Propstep, Sweet Homes, Capital Bolig, Juli
Living, C.W. Obel, and City Apartment.

## Sources watched

| Source | Area filter | Notes |
| --- | --- | --- |
| CEJ (`udlejning.cej.dk`) | Copenhagen K/V/N/Ø + Amager, ≤18,000 kr | Primary source; direct JSON API |
| Propstep | Same as CEJ | Paginated JSON API |
| Sweet Homes | Same as CEJ | HTML scrape |
| City Apartment (`cityapartment.dk`) | **Amager, København K, Frederiksberg, Vesterbro, Østerbro only** | HTML scrape |
| Capital Bolig | København V + Frederiksberg | HTML scrape + one detail-page fetch per matching listing |
| Juli Living | København K | JSON API (slow: ~6-7s per fetch) |
| C.W. Obel | Islands Brygge | HTML scrape |

All sources also pass through a shared filter: max rent 18,000 kr/month and
exclusion of Rødovre/Hvidovre/Ballerup/Valby/Vanløse.

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

This tier schedule drives **CEJ, Propstep, Sweet Homes, and City Apartment**
(the "fast" sources -- cheap, single/small-request fetches). **Capital Bolig,
Juli Living, and C.W. Obel** ("slow" sources -- either slow to fetch or one
that fans out into a detail-page request per listing) are polled on their own
independent, much slower cadence (10 minutes by default) so speeding up CEJ
doesn't multiply load on sites nobody has reported timing problems with.

## Environment Variables / Repository Secrets

- `DISCORD_WEBHOOK_URL`: The webhook URL for the Discord channel where alerts are sent.
- `DISCORD_MENTION_EVERYONE`: `true`/`false` (default `true`) -- ping `@everyone`.
- `DISCORD_MENTION` / `DISCORD_MENTION_USER_ID`: Used instead of `@everyone` when `DISCORD_MENTION_EVERYONE=false`.
- `CEJ_MAX_ATTEMPTS` / `CEJ_RETRY_BASE_SECONDS`: Retry/backoff tuning for CEJ 429/503 responses (defaults: 5 attempts, 10s base).

### Polling configuration (all optional)

- `WATCHER_ADAPTIVE_POLLING`: Set to `false` to disable the tiered schedule and poll every source at a flat `WATCHER_SLEEP_SECONDS` interval instead.
- `WATCHER_SLEEP_SECONDS`: Fixed interval used only when adaptive polling is disabled, or by the legacy single-shot mode below (default 60).
- `WATCHER_POLL_HOT_SECONDS` / `WATCHER_POLL_WARM_SECONDS` / `WATCHER_POLL_COOL_SECONDS` / `WATCHER_POLL_COLD_SECONDS`: Override any tier's fast-source interval (floored at 10s to stay polite to CEJ's origin).
- `WATCHER_SLOW_SOURCE_INTERVAL_SECONDS`: Interval between Capital Bolig/Juli Living/C.W. Obel fetches (default 600 = 10 min, floored at 60s).
- `WATCHER_MAX_RUNTIME_SECONDS`: How long a single continuous run stays alive before exiting cleanly to let the next scheduled job take over (default 4200 = 70min; the GitHub workflow sets this explicitly to match its cron interval).
- `WATCHER_RUNS`: Legacy mode. If set to a positive number, the watcher performs exactly that many polls of *every* source at a flat `WATCHER_SLEEP_SECONDS` interval, then exits, instead of the default continuous adaptive loop. Leave unset (or `0`) to use adaptive polling.

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
