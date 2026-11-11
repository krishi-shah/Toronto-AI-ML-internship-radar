"""RSS and JSON feeds."""

import json
import os
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
from core import LOOSE, STRICT, Posting, Store  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
