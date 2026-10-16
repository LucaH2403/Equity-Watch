# equity-watch

An automated watchlist service for AbbVie (ABBV) and Digital Realty Trust (DLR). It sends one daily email digest and separate urgent email alerts for relevant events. The digest summarizes recent SEC filings, company headlines, upcoming earnings dates, and daily price moves. It is informational only and does not provide investment recommendations.

## What it monitors

- **SEC filings:** recent filings for both companies from SEC EDGAR submissions data (digest covers the last seven calendar days; recent 8-K filings also trigger an urgent email).
- **Company news:** Finnhub company headlines from the last 24 hours.
- **Earnings:** Finnhub earnings calendar dates over the next 14 days.
- **Prices:** Finnhub current quote versus previous close.
- **Urgent headlines:** ABBV clinical-trial/FDA-related terms and DLR data-center, leasing, capacity, and financing-related terms. The keyword rules are deliberately visible and configurable in `URGENT_TERMS` in `equity_watch.py`; they can produce false positives or miss relevant stories.

The GitHub Actions workflow checks once an hour. It sends a digest at the first successful run of each UTC date and sends a distinct urgent email when it sees a new matching headline or a recent 8-K. A cached state file records the digest date and recently alerted event IDs to prevent duplicate mail across scheduled runs. GitHub Actions cache is best-effort: clearing caches, changing the branch name, or cache eviction can cause a repeat digest or alert.

## Setup

1. Create a Finnhub API key and configure the repository's **Settings > Secrets and variables > Actions** secrets listed below. SEC requests also require an identifying `SEC_USER_AGENT` with a real contact email. Never commit credentials.
2. Set `EMAIL_TO` to one or more comma-separated recipient addresses. Configure `SMTP_*` for an SMTP account that permits authenticated STARTTLS mail submission.
3. Ensure GitHub Actions is enabled for the repository. The workflow runs hourly (at minute 17 UTC) and can also be run manually from the Actions tab. GitHub may delay scheduled jobs, and schedules run only from the repository's default branch.

| Secret | Purpose |
| --- | --- |
| `FINNHUB_API_KEY` | Finnhub API token for company news, quotes, and earnings calendar |
| `SEC_USER_AGENT` | SEC-compliant identifying user agent, e.g. `Equity Watch your.name@example.com` |
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP STARTTLS port; defaults to `587` |
| `SMTP_USERNAME` | SMTP login name |
| `SMTP_PASSWORD` | SMTP login password or provider-specific app password |
| `EMAIL_FROM` | Sender address accepted by the SMTP account |
| `EMAIL_TO` | One or more recipients, separated by commas |

`SMTP_PORT` is optional and defaults to 587, including when GitHub passes it as an empty value.

## Run locally

Requires Python 3.10+ and no third-party packages. Set the environment variables above, then run:

```sh
python equity_watch.py --dry-run
python equity_watch.py
```

`--dry-run` fetches and prints the digest and any alerts without sending email or changing the delivery-state file. Normal execution stores state at `.state/equity-watch-state.json`; use `--state-file PATH` to choose a different path. The program uses UTC to decide daily digest delivery.

Run tests without network or email access:

```sh
python -m unittest discover -s tests -v
```

## Provider and operational notes

- **Finnhub:** one API key is used for all market/news endpoints. The workflow makes up to seven requests per run, well below Finnhub's typical free-tier per-minute limits, but plan entitlements, rate limits, API availability, and endpoint coverage can change. Review current Finnhub terms and pricing before deployment. Its news coverage is not guaranteed to be complete.
- **SEC EDGAR:** company submissions are retrieved directly from `data.sec.gov` using fixed CIKs for ABBV and DLR. Supply a descriptive user agent with a contact email, follow the SEC's current fair-access guidance, and do not increase polling frequency without checking the latest limits.
- **Email:** SMTP credentials are stored only in GitHub Actions secrets. The service requires STARTTLS; it does not send messages during tests.
- **Scheduling and state:** hourly polling provides a practical check-in cadence, not a real-time alert guarantee. GitHub scheduled runs may be delayed or dropped during high load. The Actions cache reduces duplicates but is not a transactional database or guaranteed durable store.
- **Interpretation:** market data and event feeds may be delayed, incomplete, or corrected. Headlines are matched with simple keyword rules. Nothing here is financial advice.
