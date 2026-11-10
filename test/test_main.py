import unittest
from datetime import datetime, timezone

from main import Candidate, build_report, deduplicate, extract_release_date, is_relevant, is_roundup, platform_names, roundup_titles, telugu_evidence


def item(headline, summary="", url="https://publisher.example/story", published=None, headings=()):
    return Candidate(
        headline=headline,
        summary=summary,
        feed_url="https://news.google.com/rss/search?q=test",
        article_url=url,
        publisher="publisher.example",
        published=published or datetime(2026, 11, 10, tzinfo=timezone.utc),
        headings=tuple(headings),
    )


class RadarTests(unittest.TestCase):
    def test_relevant_single_film_story(self):
        story = item("Telugu film X to stream on Netflix from November 14", "Telugu movie OTT release")
        self.assertTrue(is_relevant(story))
        self.assertFalse(is_roundup(story))

    def test_rejects_teaser_only(self):
        story = item("Telugu movie teaser released", "First look teaser")
        self.assertFalse(is_relevant(story))

    def test_detects_roundup(self):
        story = item("New Telugu OTT releases this week: several movies to watch", "Telugu movies streaming this week")
        self.assertTrue(is_roundup(story))
        self.assertTrue(is_relevant(story))

    def test_rejects_general_ott_mention_without_release_signal(self):
        story = item("Telugu actor discusses OTT business", "Telugu streaming market interview")
        self.assertFalse(is_relevant(story))

    def test_platform_detection(self):
        story = item("Telugu film premieres on Prime Video", "")
        self.assertEqual(platform_names(story), ["Prime Video"])

    def test_audio_claim_is_not_overstated(self):
        story = item("Telugu dubbed movie now streaming on Netflix", "")
        self.assertIn("not independently verified", telugu_evidence(story))

    def test_explicit_audio_wording(self):
        story = item("Film now streaming", "Audio languages include Telugu on the platform")
        self.assertIn("explicitly mentions Telugu audio", telugu_evidence(story))

    def test_release_date_requires_release_context(self):
        story = item("Telugu movie arrives on OTT", "Streaming from November 14 on Netflix")
        self.assertEqual(extract_release_date(story), "November 14")

    def test_deduplicates_same_canonical_url(self):
        first = item("Telugu movie X OTT release", url="https://publisher.example/story?ref=one")
        second = item("Telugu movie X OTT release", url="https://publisher.example/story?ref=two")
        self.assertEqual(len(deduplicate([first, second])), 1)

    def test_extracts_only_release_like_roundup_headings(self):
        story = item("Telugu OTT releases this week", "Telugu films streaming", headings=("Baa Baa Black Sheep OTT Release Date", "Read more stories", "Anakapalli streaming on aha"))
        self.assertEqual(roundup_titles(story), ["Baa Baa Black Sheep", "Anakapalli"])

    def test_report_separates_roundups(self):
        single = item("Telugu film X to stream on Netflix from November 14", "Telugu audio")
        roundup = item("Telugu OTT releases this week: movies to watch", "Telugu OTT streaming")
        report = build_report([single, roundup])
        self.assertIn("FILM-SPECIFIC STORIES", report)
        self.assertIn("WEEKLY ROUNDUP REFERENCES", report)
        self.assertIn("not a verified OTT catalogue", report)


if __name__ == "__main__":
    unittest.main()
