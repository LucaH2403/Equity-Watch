import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from equity_watch import (
    Config,
    EquityWatchError,
    format_digest,
    get_recent_filings,
    is_urgent_news,
    load_state,
    main,
    run,
    request_json,
    save_state,
    urgent_events,
)


def sample_config():
    return Config(
        finnhub_api_key="test-key",
        sec_user_agent="EquityWatch test contact@example.com",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="user",
        smtp_password="password",
        email_from="watch@example.com",
        email_to=("recipient@example.com",),
    )


class EquityWatchTests(unittest.TestCase):
    def test_config_requires_secrets_and_recipients(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(EquityWatchError, "FINNHUB_API_KEY"):
                Config.from_env()

    def test_config_parses_multiple_recipients(self):
        env = {
            "FINNHUB_API_KEY": "key",
            "SEC_USER_AGENT": "Equity Watch contact@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "secret",
            "EMAIL_FROM": "watch@example.com",
            "EMAIL_TO": "one@example.com, two@example.com",
            "SMTP_PORT": "2525",
        }
        with patch.dict("os.environ", env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.smtp_port, 2525)
        self.assertEqual(config.email_to, ("one@example.com", "two@example.com"))

    def test_config_defaults_empty_smtp_port(self):
        env = {
            "FINNHUB_API_KEY": "key",
            "SEC_USER_AGENT": "Equity Watch contact@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "secret",
            "EMAIL_FROM": "watch@example.com",
            "EMAIL_TO": "one@example.com",
            "SMTP_PORT": "",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(Config.from_env().smtp_port, 587)

    def test_email_only_config_does_not_require_provider_credentials(self):
        env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "secret",
            "EMAIL_FROM": "watch@example.com",
            "EMAIL_TO": "one@example.com",
        }
        with patch.dict("os.environ", env, clear=True):
            config = Config.from_env(require_providers=False)
        self.assertEqual(config.finnhub_api_key, "")
        self.assertEqual(config.sec_user_agent, "")

    @patch("equity_watch.urllib.request.urlopen")
    def test_provider_error_does_not_expose_query_token(self, urlopen):
        urlopen.side_effect = urllib.error.URLError("connection failed")
        with self.assertRaises(EquityWatchError) as error:
            request_json("https://api.example.com/quote?token=private-token")
        self.assertNotIn("private-token", str(error.exception))
        self.assertIn("https://api.example.com/quote", str(error.exception))

    def test_company_specific_urgent_terms(self):
        self.assertTrue(is_urgent_news("ABBV", "FDA accepts AbbVie clinical trial results"))
        self.assertFalse(is_urgent_news("ABBV", "AbbVie reports quarterly earnings"))
        self.assertTrue(is_urgent_news("DLR", "Data center leasing demand increases"))
        self.assertFalse(is_urgent_news("DLR", "Digital Realty announces board meeting"))
        self.assertFalse(is_urgent_news("DLR", "Digital Realty announces a release"))

    def test_recent_filings_exclude_old_records_and_link_to_sec(self):
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        submission = {
            "cik": "1551152",
            "filings": {"recent": {
                "filingDate": ["2026-10-15", "2026-10-01"],
                "form": ["8-K", "10-Q"],
                "accessionNumber": ["0001551152-26-000001", "0001551152-26-000002"],
                "primaryDocument": ["report.htm", "quarterly.htm"],
            }},
        }
        filings = get_recent_filings(submission, 7, now)
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0]["form"], "8-K")
        self.assertIn("/Archives/edgar/data/1551152/000155115226000001/report.htm", filings[0]["url"])

    def test_digest_contains_prices_filings_news_earnings_and_disclaimer(self):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        text = format_digest(
            now,
            {"ABBV": [{"headline": "FDA update", "datetime": now.timestamp(), "url": "https://example.com"}],
             "DLR": []},
            {"ABBV": {"c": 200, "pc": 190}, "DLR": {"c": 150, "pc": 150}},
            {"ABBV": [{"form": "8-K", "date": "2026-10-16", "url": "https://sec.gov/a"}], "DLR": []},
            [{"symbol": "DLR", "date": "2026-10-20"}],
        )
        self.assertIn("not investment advice", text)
        self.assertIn("ABBV: $200.00 (+5.26%", text)
        self.assertIn("8-K filed 2026-10-16", text)
        self.assertIn("FDA update", text)
        self.assertIn("DLR: 2026-10-20", text)

    def test_urgent_events_include_recent_relevant_headlines_and_8k(self):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        news = {
            "ABBV": [{"headline": "FDA grants approval", "datetime": now.timestamp(), "url": "https://example.com/a"}],
            "DLR": [{"headline": "Board meeting scheduled", "datetime": now.timestamp(), "url": "https://example.com/b"}],
        }
        filings = {
            "ABBV": [{"form": "8-K", "date": "2026-10-16", "id": "filing:one", "url": "https://sec.gov/a"}],
            "DLR": [],
        }
        alerts = urgent_events(news, filings, now)
        self.assertEqual(len(alerts), 2)
        self.assertIn("FDA grants approval", alerts[0][1])
        self.assertEqual(alerts[1][0], "filing:one")

    def test_state_round_trip_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {"digest_sent_date": "2026-10-16", "sent_alert_ids": ["news:ABBV:a"]}
            save_state(path, state)
            self.assertEqual(load_state(path), state)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(EquityWatchError):
                load_state(path)

    @patch("equity_watch.collect_snapshot")
    @patch("equity_watch.send_email")
    def test_run_sends_unique_urgent_and_daily_digest_once(self, send, collect):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        collect.return_value = (
            {"ABBV": [{"headline": "FDA approval", "datetime": now.timestamp(), "url": "https://example.com/a"}],
             "DLR": []},
            {"ABBV": {"c": 100, "pc": 99}, "DLR": {"c": 100, "pc": 100}},
            {"ABBV": [], "DLR": []},
            [],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            output = run(sample_config(), path, now)
            self.assertIn("URGENT ALERTS", output)
            self.assertEqual(send.call_count, 2)
            send.reset_mock()
            run(sample_config(), path, now + timedelta(hours=1))
            self.assertEqual(send.call_count, 0)

    @patch("equity_watch.collect_snapshot")
    @patch("equity_watch.send_email")
    def test_dry_run_never_sends_or_persists(self, send, collect):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        collect.return_value = (
            {"ABBV": [], "DLR": []},
            {"ABBV": {}, "DLR": {}},
            {"ABBV": [], "DLR": []},
            [],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            output = run(sample_config(), path, now, dry_run=True)
            self.assertIn("DAILY DIGEST EMAIL", output)
            send.assert_not_called()
            self.assertFalse(path.exists())

    @patch("equity_watch.send_email")
    @patch("equity_watch.collect_snapshot")
    def test_test_email_sends_without_fetching_or_changing_state(self, collect, send):
        env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "secret",
            "EMAIL_FROM": "watch@example.com",
            "EMAIL_TO": "recipient@example.com",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(main(["--test-email"]), 0)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "Equity Watch test email")
        collect.assert_not_called()

    @patch("equity_watch.collect_snapshot")
    @patch("equity_watch.send_email")
    def test_email_failure_does_not_mark_digest_sent(self, send, collect):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        collect.return_value = (
            {"ABBV": [], "DLR": []},
            {"ABBV": {}, "DLR": {}},
            {"ABBV": [], "DLR": []},
            [],
        )
        send.side_effect = EquityWatchError("mail failed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with self.assertRaises(EquityWatchError):
                run(sample_config(), path, now)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
