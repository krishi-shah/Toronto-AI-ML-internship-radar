"""Tests for the email backend. The SMTP client is mocked; nothing is sent."""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import notify  # noqa: E402
from core import Posting, Verdict  # noqa: E402

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "radar",
    "SMTP_PASSWORD": "secret",
    "EMAIL_FROM": "radar@example.com",
    "EMAIL_TO": "me@example.com",
}


def hit(title="Machine Learning Intern", company="Cohere"):
    post = Posting(company=company, title=title, location="Toronto, Ontario",
                   url="https://example.com/j/1", uid=f"t:{title}")
    return post, Verdict(tier="strict", reason="test")


class EmailBackendTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("RADAR_NOTIFIERS", None)

    def tearDown(self):
        os.environ.pop("RADAR_NOTIFIERS", None)
        if self._saved is not None:
            os.environ["RADAR_NOTIFIERS"] = self._saved

    def test_skipped_when_settings_missing(self):
        env = {k: v for k, v in ENV.items() if k != "SMTP_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(notify.smtplib, "SMTP") as smtp:
            built = notify.build_notifiers(["email"], None)
        self.assertEqual(built, [])
        smtp.assert_not_called()

    def test_built_when_settings_present(self):
        with mock.patch.dict(os.environ, ENV, clear=True):
            built = notify.build_notifiers(["email"], None)
        self.assertEqual([n.name for n in built], ["email"])

    def test_strict_sends_one_message_over_starttls(self):
        with mock.patch.dict(os.environ, ENV, clear=True), \
                mock.patch.object(notify.smtplib, "SMTP") as smtp_cls:
            notifier = notify.build_notifiers(["email"], None)[0]
            self.assertTrue(notifier.strict([hit(), hit("ML Co-op", "Shopify")]))

        smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
        smtp = smtp_cls.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("radar", "secret")
        smtp.send_message.assert_called_once()
        msg = smtp.send_message.call_args[0][0]
        self.assertEqual(msg["Subject"], "Radar: 2 new AI/ML roles")
        self.assertEqual(msg["To"], "me@example.com")
        body = msg.get_content()
        self.assertIn("Machine Learning Intern at Cohere", body)
        self.assertIn("ML Co-op at Shopify", body)

    def test_empty_strict_sends_nothing(self):
        with mock.patch.dict(os.environ, ENV, clear=True), \
                mock.patch.object(notify.smtplib, "SMTP") as smtp_cls:
            notifier = notify.build_notifiers(["email"], None)[0]
            self.assertFalse(notifier.strict([]))
        smtp_cls.assert_not_called()

    def test_digest_and_health_are_not_sent_by_email(self):
        with mock.patch.dict(os.environ, ENV, clear=True), \
                mock.patch.object(notify.smtplib, "SMTP") as smtp_cls:
            notifier = notify.build_notifiers(["email"], None)[0]
            self.assertFalse(notifier.digest([{"uid": "x"}]))
            self.assertFalse(notifier.health("down"))
        smtp_cls.assert_not_called()

    def test_send_failure_returns_false(self):
        with mock.patch.dict(os.environ, ENV, clear=True), \
                mock.patch.object(notify.smtplib, "SMTP", side_effect=OSError("refused")):
            notifier = notify.build_notifiers(["email"], None)[0]
            self.assertFalse(notifier.strict([hit()]))

    def test_scheduled_run_without_secrets_stays_quiet(self):
        # Regression: the workflow now lists email in RADAR_NOTIFIERS. Without
        # the secrets, readme and feed must still build and nothing is printed.
        env = {"RADAR_NOTIFIERS": "readme,feed,email"}
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            built = notify.build_notifiers([], None)
        self.assertEqual([n.name for n in built], ["readme", "feed"])
        self.assertEqual(out.getvalue(), "")

    def test_partial_email_settings_warn(self):
        env = {k: v for k, v in ENV.items() if k != "SMTP_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            built = notify.build_notifiers(["email"], None)
        self.assertEqual(built, [])
        self.assertIn("email needs", out.getvalue())

    def test_workflow_maps_each_smtp_secret(self):
        path = os.path.join(ROOT, ".github", "workflows", "radar.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                    "EMAIL_FROM", "EMAIL_TO"):
            self.assertRegex(text, rf"(?m)^\s+{key}: \$\{{\{{ secrets\.{key} \}}\}}\s*$")
        self.assertRegex(text, r"RADAR_NOTIFIERS: readme,feed,email")

    def test_radar_notifiers_override_enables_email(self):
        env = {**ENV, "RADAR_NOTIFIERS": "readme,feed,email"}
        with mock.patch.dict(os.environ, env, clear=True):
            built = notify.build_notifiers(["dashboard"], None)
        self.assertIn("email", [n.name for n in built])
        self.assertNotIn("dashboard", [n.name for n in built])


if __name__ == "__main__":
    unittest.main()
