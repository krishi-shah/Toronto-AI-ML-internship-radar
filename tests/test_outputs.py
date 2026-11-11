"""RSS/JSON feed and Discord alerts."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
from core import LOOSE, STRICT, Posting, Store, classify  # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "r.db"))
        now = int(time.time())
        for uid, title, tier in [("a", "Machine Learning Intern & Co-op", STRICT),
                                 ("b", "Software Developer Co-op", LOOSE)]:
            post = Posting("Acme <AI>", title, "Toronto, ON", f"https://e.com/{uid}?x=1&y=2", uid,
                           posted_at=now - 3600)
            self.store.record(post, tier, "t")
        self.store.commit()

    def tearDown(self):
        self.store.close()


class TestFeedWriter(StoreCase):
    def paths(self):
        return os.path.join(self.dir, "feed.json"), os.path.join(self.dir, "feed.xml")

    def test_writes_valid_json_and_rss(self):
        j, x = self.paths()
        self.assertTrue(notify.FeedWriter(self.store, j, x).render())
        with open(j) as fh:
            data = json.load(fh)
        self.assertEqual(data["count"], 2)
        self.assertEqual([i["tier"] for i in data["jobs"]], ["strict", "loose"])
        root = ET.parse(x).getroot()
        items = root.findall("./channel/item")
        self.assertEqual(len(items), 2)
        self.assertIn("Acme <AI>", items[0].find("title").text)
        self.assertEqual(items[0].find("link").text, "https://e.com/a?x=1&y=2")

    def test_unchanged_feed_is_not_rewritten(self):
        j, x = self.paths()
        writer = notify.FeedWriter(self.store, j, x)
        writer.render()
        before = (os.path.getmtime(j), os.path.getmtime(x))
        time.sleep(0.05)
        writer.render()
        self.assertEqual(before, (os.path.getmtime(j), os.path.getmtime(x)))


class TestDiscord(unittest.TestCase):
    def hit(self, n=1):
        post = Posting("Acme", "ML Intern (Summer 2027)", "Toronto, ON", "https://e.com/1", "u")
        return [(post, classify(post))] * n

    def test_skipped_without_webhook(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": ""}), mock.patch("builtins.print"):
            built = notify.build_notifiers(["discord"], None)
        self.assertEqual(built, [])

    def test_posts_embeds_in_batches_of_ten(self):
        ok = mock.Mock(status_code=204, text="")
        with mock.patch("requests.post", return_value=ok) as post, mock.patch("time.sleep"):
            self.assertTrue(notify.DiscordNotifier("https://discord/webhook").strict(self.hit(12)))
        self.assertEqual(post.call_count, 2)
        first = post.call_args_list[0].kwargs["json"]
        self.assertEqual(len(first["embeds"]), 10)
        self.assertIn("12 new AI/ML roles", first["content"])
        self.assertEqual(first["allowed_mentions"], {"parse": []})
        self.assertEqual(first["embeds"][0]["fields"][-1], {"name": "Term", "value": "Summer 2027", "inline": True})

    def test_empty_run_posts_nothing(self):
        with mock.patch("requests.post") as post:
            self.assertTrue(notify.DiscordNotifier("https://discord/webhook").strict([]))
        post.assert_not_called()

    def test_failure_is_reported(self):
        bad = mock.Mock(status_code=404, text="Unknown Webhook")
        with mock.patch("requests.post", return_value=bad), mock.patch("time.sleep"), \
                mock.patch("builtins.print"):
            self.assertFalse(notify.DiscordNotifier("https://discord/webhook").strict(self.hit()))


if __name__ == "__main__":
    unittest.main()
