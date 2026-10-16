#!/usr/bin/env python3
"""Daily ABBV and DLR market digest and urgent email alerts."""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any


COMPANIES = {
    "ABBV": {"name": "AbbVie", "cik": "0001551152"},
    "DLR": {"name": "Digital Realty Trust", "cik": "0001297996"},
}
FINNHUB_API = "https://finnhub.io/api/v1"
SEC_API = "https://data.sec.gov/submissions"
REQUEST_TIMEOUT = 20
STATE_PATH = Path(".state/equity-watch-state.json")
ALERT_HISTORY_LIMIT = 500

URGENT_TERMS = {
    "ABBV": (
        "clinical trial",
        "clinical study",
        "phase 1",
        "phase 2",
        "phase 3",
        "fda",
        "food and drug administration",
        "approval",
        "approved",
        "rejection",
        "rejected",
        "complete response letter",
        "black box",
        "safety alert",
    ),
    "DLR": (
        "data center",
        "data centre",
        "leasing",
        "lease",
        "capacity",
        "colocation",
        "hyperscale",
        "financing",
        "debt offering",
        "refinancing",
        "joint venture",
        "acquisition",
    ),
}


class EquityWatchError(Exception):
    """An expected configuration, provider, or delivery error."""


@dataclass(frozen=True)
class Config:
    finnhub_api_key: str
    sec_user_agent: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        required = (
            "FINNHUB_API_KEY",
            "SEC_USER_AGENT",
            "SMTP_HOST",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "EMAIL_FROM",
            "EMAIL_TO",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise EquityWatchError(f"Missing required environment variables: {', '.join(missing)}")
        try:
            smtp_port = int(os.environ.get("SMTP_PORT", "").strip() or "587")
        except ValueError as exc:
            raise EquityWatchError("SMTP_PORT must be an integer") from exc
        if not 1 <= smtp_port <= 65535:
            raise EquityWatchError("SMTP_PORT must be between 1 and 65535")
        recipients = tuple(item.strip() for item in os.environ["EMAIL_TO"].split(",") if item.strip())
        if not recipients:
            raise EquityWatchError("EMAIL_TO must contain at least one email address")
        return cls(
            finnhub_api_key=os.environ["FINNHUB_API_KEY"].strip(),
            sec_user_agent=os.environ["SEC_USER_AGENT"].strip(),
            smtp_host=os.environ["SMTP_HOST"].strip(),
            smtp_port=smtp_port,
            smtp_username=os.environ["SMTP_USERNAME"].strip(),
            smtp_password=os.environ["SMTP_PASSWORD"],
            email_from=os.environ["EMAIL_FROM"].strip(),
            email_to=recipients,
        )


def request_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        parts = urllib.parse.urlsplit(url)
        safe_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        raise EquityWatchError(f"Could not retrieve {safe_url} ({type(exc).__name__})") from exc


class Providers:
    def __init__(self, config: Config):
        self.config = config

    def finnhub(self, endpoint: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode({**params, "token": self.config.finnhub_api_key})
        result = request_json(f"{FINNHUB_API}/{endpoint}?{query}")
        if isinstance(result, dict) and result.get("error"):
            raise EquityWatchError(f"Finnhub {endpoint} error: {result['error']}")
        return result

    def sec_submissions(self, cik: str) -> dict[str, Any]:
        result = request_json(
            f"{SEC_API}/CIK{cik}.json",
            {"User-Agent": self.config.sec_user_agent},
        )
        if not isinstance(result, dict):
            raise EquityWatchError(f"SEC submissions response for CIK {cik} was not an object")
        return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_day(value: datetime) -> str:
    return value.astimezone(timezone.utc).date().isoformat()


def news_datetime(item: dict[str, Any]) -> datetime | None:
    timestamp = item.get("datetime")
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc)


def news_identity(symbol: str, item: dict[str, Any]) -> str:
    stable = item.get("url") or f"{item.get('datetime', '')}|{item.get('headline', '')}"
    return f"news:{symbol}:{stable}"


def get_recent_filings(submissions: dict[str, Any], days: int, now: datetime) -> list[dict[str, str]]:
    recent = submissions.get("filings", {}).get("recent", {})
    dates = recent.get("filingDate", [])
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    documents = recent.get("primaryDocument", [])
    cutoff = now.date() - timedelta(days=days)
    results = []
    for filing_date, form, accession, document in zip(dates, forms, accessions, documents):
        try:
            parsed_date = date.fromisoformat(filing_date)
        except (TypeError, ValueError):
            continue
        if parsed_date < cutoff:
            continue
        clean_accession = accession.replace("-", "")
        cik = submissions.get("cik")
        if not cik:
            continue
        cik_int = str(int(cik))
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{clean_accession}/{urllib.parse.quote(document)}"
        results.append({"date": filing_date, "form": form, "url": url, "id": f"filing:{accession}"})
    return results


def is_urgent_news(symbol: str, headline: str) -> bool:
    lowered = headline.casefold()
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in URGENT_TERMS[symbol])


def format_digest(
    now: datetime,
    news: dict[str, list[dict[str, Any]]],
    quotes: dict[str, dict[str, Any]],
    filings: dict[str, list[dict[str, str]]],
    earnings: list[dict[str, Any]],
) -> str:
    lines = [
        f"Equity Watch daily digest - {iso_day(now)} (UTC)",
        "",
        "For information only; this digest is not investment advice.",
        "",
        "DAILY PRICE MOVES",
    ]
    for symbol in COMPANIES:
        quote = quotes.get(symbol, {})
        current, previous = quote.get("c"), quote.get("pc")
        if isinstance(current, (int, float)) and isinstance(previous, (int, float)) and previous:
            change = (current - previous) / previous * 100
            lines.append(f"- {symbol}: ${current:.2f} ({change:+.2f}% vs. previous close)")
        else:
            lines.append(f"- {symbol}: price data unavailable")

    lines.extend(("", "SEC FILINGS (last 7 days)"))
    for symbol in COMPANIES:
        entries = filings.get(symbol, [])
        if not entries:
            lines.append(f"- {symbol}: no recent filings found")
        for entry in entries:
            lines.append(f"- {symbol} {entry['form']} filed {entry['date']}: {entry['url']}")

    lines.extend(("", "COMPANY NEWS (last 24 hours)"))
    for symbol in COMPANIES:
        recent = [
            item for item in news.get(symbol, [])
            if (published := news_datetime(item)) is not None and now - timedelta(hours=24) <= published <= now
        ]
        lines.append(f"- {symbol}:")
        if not recent:
            lines.append("  No company headlines found.")
        for item in recent:
            headline = str(item.get("headline") or "Untitled headline").strip()
            url = str(item.get("url") or "")
            source = str(item.get("source") or "").strip()
            suffix = f" - {source}" if source else ""
            lines.append(f"  - {headline}{suffix}" + (f" ({url})" if url else ""))

    upcoming = [
        item for item in earnings
        if item.get("symbol") in COMPANIES and isinstance(item.get("date"), str)
        and iso_day(now) <= item["date"] <= (now.date() + timedelta(days=14)).isoformat()
    ]
    lines.extend(("", "UPCOMING EARNINGS (next 14 days)"))
    if not upcoming:
        lines.append("- No ABBV or DLR earnings dates found.")
    else:
        for item in sorted(upcoming, key=lambda entry: (entry["date"], entry["symbol"])):
            lines.append(f"- {item['symbol']}: {item['date']}")
    return "\n".join(lines)


def collect_snapshot(providers: Providers, now: datetime) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, str]]],
    list[dict[str, Any]],
]:
    today = now.date()
    news_start = (today - timedelta(days=1)).isoformat()
    news_end = today.isoformat()
    news: dict[str, list[dict[str, Any]]] = {}
    quotes: dict[str, dict[str, Any]] = {}
    filings: dict[str, list[dict[str, str]]] = {}
    for symbol, company in COMPANIES.items():
        company_news = providers.finnhub(
            "company-news", {"symbol": symbol, "from": news_start, "to": news_end}
        )
        if not isinstance(company_news, list):
            raise EquityWatchError(f"Finnhub company-news response for {symbol} was not a list")
        news[symbol] = company_news
        quote = providers.finnhub("quote", {"symbol": symbol})
        if not isinstance(quote, dict):
            raise EquityWatchError(f"Finnhub quote response for {symbol} was not an object")
        quotes[symbol] = quote
        submissions = providers.sec_submissions(company["cik"])
        filings[symbol] = get_recent_filings(submissions, 7, now)

    earnings_result = providers.finnhub(
        "calendar/earnings",
        {"from": today.isoformat(), "to": (today + timedelta(days=14)).isoformat()},
    )
    earnings = earnings_result.get("earningsCalendar", []) if isinstance(earnings_result, dict) else None
    if not isinstance(earnings, list):
        raise EquityWatchError("Finnhub earnings calendar response was not a list")
    return news, quotes, filings, earnings


def urgent_events(
    news: dict[str, list[dict[str, Any]]],
    filings: dict[str, list[dict[str, str]]],
    now: datetime,
) -> list[tuple[str, str]]:
    events = []
    for symbol, items in news.items():
        for item in items:
            published = news_datetime(item)
            headline = str(item.get("headline") or "").strip()
            if (
                published is not None
                and now - timedelta(hours=24) <= published <= now
                and is_urgent_news(symbol, headline)
            ):
                url = str(item.get("url") or "")
                events.append((news_identity(symbol, item), f"{symbol}: {headline}" + (f"\n{url}" if url else "")))
    cutoff = now.date() - timedelta(days=1)
    for symbol, entries in filings.items():
        for entry in entries:
            if entry["form"] == "8-K" and date.fromisoformat(entry["date"]) >= cutoff:
                events.append((entry["id"], f"{symbol}: 8-K filed {entry['date']}\n{entry['url']}"))
    return events


def send_email(config: Config, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = ", ".join(config.email_to)
    message.set_content(body)
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=REQUEST_TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(config.smtp_username, config.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EquityWatchError(f"Could not send email: {exc}") from exc


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"digest_sent_date": None, "sent_alert_ids": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EquityWatchError(f"Could not read state file {path}: {exc}") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("sent_alert_ids", []), list)
        or any(not isinstance(item, str) for item in state.get("sent_alert_ids", []))
    ):
        raise EquityWatchError(f"State file {path} has an invalid format")
    state.setdefault("digest_sent_date", None)
    state.setdefault("sent_alert_ids", [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    except OSError as exc:
        raise EquityWatchError(f"Could not save state file {path}: {exc}") from exc


def run(config: Config, state_path: Path, now: datetime, dry_run: bool = False) -> str:
    news, quotes, filings, earnings = collect_snapshot(Providers(config), now)
    digest = format_digest(now, news, quotes, filings, earnings)
    state = load_state(state_path)

    events = urgent_events(news, filings, now)
    known_ids = set(state["sent_alert_ids"])
    new_events = [(event_id, body) for event_id, body in events if event_id not in known_ids]
    output = [digest]
    if new_events:
        alert_body = "Urgent company event(s) for your watchlist:\n\n" + "\n\n".join(
            f"- {body}" for _, body in new_events
        )
        output.extend(("", "URGENT ALERTS", alert_body))
        if not dry_run:
            send_email(config, "URGENT: Equity Watch - ABBV / DLR", alert_body)
            state["sent_alert_ids"] = (state["sent_alert_ids"] + [event_id for event_id, _ in new_events])[
                -ALERT_HISTORY_LIMIT:
            ]
            save_state(state_path, state)

    day = iso_day(now)
    if state["digest_sent_date"] != day:
        output.extend(("", "DAILY DIGEST EMAIL", "A daily digest email is due."))
        if not dry_run:
            send_email(config, f"Equity Watch daily digest - {day}", digest)
            state["digest_sent_date"] = day
            save_state(state_path, state)
    elif not dry_run:
        output.append("\nDaily digest already sent for this UTC day.")
    return "\n".join(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print the digest without sending email or changing state")
    parser.add_argument("--state-file", type=Path, default=STATE_PATH, help="Path to the JSON state file")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        print(run(config, args.state_file, utc_now(), args.dry_run))
    except EquityWatchError as exc:
        print(f"equity-watch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
