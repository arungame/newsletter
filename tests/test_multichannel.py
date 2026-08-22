import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ainews.alerting import AlertNotifier
from ainews.channels import (
    AdapterRegistry,
    CallbackPublisherAdapter,
    ChannelCapabilities,
    load_channel_configs,
)
from ainews.config import Settings
from ainews.issues import IssueBuilder
from ainews.publisher import DigestPublisher, PublicationResult
from ainews.repository import ArticleRepository
from ainews.service import NewsService
from ainews.tavily import TavilyCollector


def canonical_payload(item_count: int = 7) -> dict:
    articles = []
    selection = []
    for index in range(item_count):
        article_id = index + 1
        articles.append(
            {
                "id": article_id,
                "title": f"AI story {article_id}",
                "display_title_zh": f"AI 资讯 {article_id}",
                "compact_summary_zh": f"摘要 {article_id}",
                "source_name": "Primary Source",
                "url": f"https://example.com/{article_id}",
                "published_at": "2026-08-21T12:00:00+00:00",
                "topic": "news",
                "rank_score": 100 - index,
            }
        )
        selection.append(
            {
                "article_id": article_id,
                "title": f"AI 资讯 {article_id}",
                "source_name": "Primary Source",
                "published_at": "2026-08-21T12:00:00+00:00",
                "rank_score": 100 - index,
            }
        )
    payload = {
        "region": "all",
        "since_hours": 24,
        "articles": articles,
        "selection_preview": selection,
        "editor_snapshot": {"version": 3},
        "digest": {
            "title": "AI 日报",
            "overview": "过去 24 小时最重要的 AI 进展。",
            "highlights": ["重点一", "重点二"],
            "sections": [],
            "closing": "",
        },
    }
    issue = IssueBuilder(
        public_base_url="https://newsletter.example.com",
        min_items=5,
        max_items=10,
    ).build(payload, window_end=datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc))
    payload["canonical_issue"] = issue.to_dict()
    return payload


class MultichannelTestCase(unittest.TestCase):
    def test_issue_builder_enforces_minimum_and_maximum(self) -> None:
        blocked = canonical_payload(4)["canonical_issue"]
        ready = canonical_payload(12)["canonical_issue"]

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(len(ready["items"]), 10)
        self.assertTrue(ready["public_url"].endswith("/issues/2026-08-22"))

    def test_static_site_archive_feed_and_html_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                database_path=Path(temp_dir) / "db.sqlite",
                sources_file=Path(temp_dir) / "sources.json",
                output_dir=Path(temp_dir) / "output",
                static_site_dir=Path(temp_dir) / "site",
                channels_file=Path(temp_dir) / "missing.yaml",
                public_base_url="https://newsletter.example.com",
            )
            payload = canonical_payload()
            payload["canonical_issue"]["items"][0]["source_url"] = "javascript:alert(1)"
            result = DigestPublisher(settings).publish(payload, targets=["static_site"])

            self.assertEqual(result["status"], "ok")
            issue_path = settings.static_site_dir / "issues" / "2026-08-22" / "index.html"
            rendered = issue_path.read_text(encoding="utf-8")
            self.assertNotIn("javascript:alert", rendered)
            self.assertTrue((settings.static_site_dir / "feed.xml").exists())
            self.assertTrue((settings.static_site_dir / "robots.txt").exists())
            self.assertIn(
                "/issues/2026-08-22", (settings.static_site_dir / "index.html").read_text()
            )

    def test_manual_export_contains_canonical_copy_and_tracked_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                database_path=Path(temp_dir) / "db.sqlite",
                sources_file=Path(temp_dir) / "sources.json",
                output_dir=Path(temp_dir) / "output",
                static_site_dir=Path(temp_dir) / "site",
                channels_file=Path(temp_dir) / "missing.yaml",
            )
            result = DigestPublisher(settings).publish(
                canonical_payload(), targets=["manual_export"]
            )
            zip_path = Path(result["targets"][0]["files"][0])
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "caption.txt",
                        "issue.json",
                        "cover.svg",
                        "summary-card.svg",
                        "README.txt",
                    },
                )
                caption = archive.read("caption.txt").decode("utf-8")
                issue = json.loads(archive.read("issue.json"))
            self.assertIn("utm_source=manual_export", caption)
            self.assertEqual(len(issue["items"]), 7)

    def test_new_channel_is_added_by_adapter_and_config_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AdapterRegistry()
            seen = []

            def publish_callback(variant, config):
                seen.append((variant.title, config.name))
                return PublicationResult(target=config.name, status="ok", external_id="TEST123")

            registry.register(
                CallbackPublisherAdapter(
                    "test_channel",
                    capabilities=ChannelCapabilities(automatic_publish=True),
                    publish_callback=publish_callback,
                )
            )
            settings = Settings(
                database_path=Path(temp_dir) / "db.sqlite",
                sources_file=Path(temp_dir) / "sources.json",
                output_dir=Path(temp_dir) / "output",
                static_site_dir=Path(temp_dir) / "site",
                channels_file=Path(temp_dir) / "missing.yaml",
            )
            result = DigestPublisher(settings, adapter_registry=registry).publish(
                canonical_payload(), targets=["test_channel"]
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(seen, [("AI 日报", "test_channel")])

    def test_channel_validation_idempotency_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            channel_path = Path(temp_dir) / "channels.yaml"
            channel_path.write_text(
                "channels:\n  wechat:\n    enabled: true\n    mode: auto\n    account_id: primary\n",
                encoding="utf-8",
            )
            self.assertEqual(load_channel_configs(channel_path)["wechat"].mode, "auto")

            repository = ArticleRepository(Path(temp_dir) / "db.sqlite")
            first = repository.save_publication(
                digest_id=None,
                target="test",
                idempotency_key="issue:test:primary:1",
                status="ok",
                response_payload={"access_token": "secret", "nested": {"password": "hidden"}},
            )
            second = repository.save_publication(
                digest_id=None,
                target="test",
                idempotency_key="issue:test:primary:1",
                status="ok",
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(first["response_payload"]["access_token"], "***")
            self.assertEqual(first["response_payload"]["nested"]["password"], "***")

    def test_wechat_auto_mode_requires_manual_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            channel_path = Path(temp_dir) / "channels.yaml"
            channel_path.write_text(
                "channels:\n  wechat:\n    enabled: true\n    mode: review\n    account_id: primary\n",
                encoding="utf-8",
            )
            settings = Settings(
                database_path=Path(temp_dir) / "db.sqlite",
                sources_file=Path(temp_dir) / "sources.json",
                channels_file=channel_path,
            )
            service = NewsService(settings)

            with self.assertRaisesRegex(ValueError, "successful review days"):
                service.set_channel_mode("wechat", "auto")
            self.assertEqual(load_channel_configs(channel_path)["wechat"].mode, "review")

            settings.wechat_allow_early_auto = True
            updated = service.set_channel_mode("wechat", "auto")
            self.assertEqual(updated["mode"], "auto")

    def test_tavily_gap_filler_returns_normalized_articles(self) -> None:
        calls = []

        def fake_post(url, payload, **kwargs):
            calls.append((url, payload))
            return {
                "results": [
                    {
                        "title": "New AI system",
                        "url": "https://research.example.com/new-ai",
                        "content": "Primary research announcement.",
                        "published_date": "2026-08-21T12:00:00Z",
                    }
                ]
            }

        articles = TavilyCollector("test-key", json_post=fake_post).collect(["AI news"])

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source_id, "tavily-search")
        self.assertEqual(articles[0].raw_payload["query"], "AI news")
        self.assertEqual(calls[0][1]["topic"], "news")

    def test_smtp_alert_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sent = []

            class FakeSMTP:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def login(self, username, password):
                    sent.append(("login", username, password))

                def send_message(self, message):
                    sent.append(("message", message["To"], message.get_content()))

            settings = Settings(
                database_path=Path(temp_dir) / "db.sqlite",
                sources_file=Path(temp_dir) / "sources.json",
                alert_targets="email",
                smtp_host="smtp.example.com",
                smtp_username="newsletter@example.com",
                smtp_password="secret",
                smtp_from="newsletter@example.com",
                smtp_to="admin@example.com",
            )
            repository = ArticleRepository(settings.database_path)
            notifier = AlertNotifier(
                settings,
                repository,
                smtp_factory=lambda *args, **kwargs: FakeSMTP(),
            )
            result = notifier.notify_rule(
                "daily-preview", active=True, title="审核稿已生成", message="请检查微信预览"
            )

            self.assertEqual(result["status"], "sent")
            self.assertEqual(sent[1][1], "admin@example.com")


if __name__ == "__main__":
    unittest.main()
