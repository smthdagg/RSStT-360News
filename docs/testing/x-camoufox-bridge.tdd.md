# X Camoufox Bridge - TDD Evidence

## Source and user journey

The journey was derived during this TDD run: as a 360News user, I want X feeds
to use one persistent browser session and back off after failures so that the X
account is not repeatedly logged out or rate-limited.

## RED / GREEN report

- RED command: `.venv/bin/python -m unittest tests.test_x_browser_fetcher -v`
- RED result: `ModuleNotFoundError: No module named 'src.x_browser_fetcher'`
- GREEN command: `.venv/bin/python -m unittest discover -s tests -v`
- GREEN result: `Ran 10 tests ... OK`
- Coverage command: `.venv/bin/python -m coverage run --source=src.x_browser_fetcher -m unittest tests.test_x_browser_fetcher && .venv/bin/python -m coverage report -m`
- Coverage result: `src/x_browser_fetcher.py: 234 statements, 43 missed, 82%`

## Test specification

| # | What is guaranteed | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Cookie imports accept only exact `x.com` and `twitter.com` domains and deduplicate repeated authentication cookies | `CookieSanitizingTests` | unit | PASS |
| 2 | Browser-captured `UserTweets` payloads, including visibility wrappers and long posts, become the stable RSS tweet shape | `TimelineParsingTests` | unit | PASS |
| 3 | Failed cache entries wait for the configured backoff instead of immediately starting another 47-account scan | `SchedulingTests` | unit | PASS |
| 4 | HTTP authentication failures and rate limits are classified separately | `ResponseClassificationTests` | unit | PASS |
| 5 | Camoufox persists one fingerprint/profile, imports a clean session once, validates usernames, and saves only X cookies | `BrowserFetcherTests` | integration with browser mocks | PASS |

## Live verification

- Camoufox `v152.0.4-beta.28` on macOS ARM64 returned HTTP 200 for live X profile timelines.
- `@elonmusk` returned 20 parsed posts; the first observed post timestamp was `2026-07-21T04:56:59+00:00`.
- The running bridge health endpoint reported `backend=Camoufox`, an authenticated session, no rate limit, and 11 successful account caches during the observed first pass.
- `http://127.0.0.1:1200/twitter/user/0xcryptowizard` returned a valid RSS document containing 17 items.

## Known gaps

The local 360News Bot process currently exits before polling feeds because its
Telegram `API_ID` / `API_HASH` are not configured. This is separate from the X
browser bridge; no credentials were created or changed during this task.
