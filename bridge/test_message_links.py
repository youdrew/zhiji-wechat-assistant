"""Synthetic shared-card parsing and source projection; no WeChat data or network.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import unittest
from types import SimpleNamespace
from xml.sax.saxutils import escape

from backend_contracts import QUOTED_REPLY_TYPE
from chat_server import classify
from message_links import MAX_URL_LENGTH, MAX_XML_LENGTH, link_metadata
from wechat_source import WeChatSource


def card(subtype=5, title="🌿 合成分享：周末散步", description="合成分享说明", url="https://example.invalid/note?a=1&b=2", extra=""):
    target = "<url>" + escape(url) + "</url>" if url is not None else ""
    return (f"<msg><appmsg><type>{subtype}</type><title>{escape(title)}</title>"
            f"<des>{escape(description)}</des>{target}{extra}</appmsg>"
            "<appinfo><appname>小红书</appname></appinfo></msg>")


class SharedLinkMetadataTests(unittest.TestCase):
    def test_link_fields_preserve_emoji_cdata_and_xml_entities(self):
        xml = card().replace("<des>合成分享说明</des>", "<des><![CDATA[<b>合成</b> & 说明]]></des>")
        self.assertEqual(link_metadata(49, xml), {
            "title": "🌿 合成分享：周末散步", "description": "<b>合成</b> & 说明",
            "url": "https://example.invalid/note?a=1&b=2", "source": "小红书"})

    def test_sender_prefix_declaration_and_bare_appmsg_are_supported(self):
        for xml in ("synthetic-user:\n" + card(), '<?xml version="1.0"?>' + card(),
                    '<appmsg><type>5</type><title>分享</title><url>https://example.invalid</url></appmsg>'):
            with self.subTest(xml=xml):
                self.assertIsNotNone(link_metadata(49, xml))

    def test_both_packed_and_xml_subtypes_are_supported(self):
        for subtype in (5, 33, 36):
            for kind, xml in ((49, card(subtype)), ((subtype << 32) | 49, card(subtype)),
                              ((subtype << 32) | 49, card(subtype).replace(f"<type>{subtype}</type>", ""))):
                with self.subTest(kind=kind):
                    self.assertEqual(link_metadata(kind, xml)["url"], "https://example.invalid/note?a=1&b=2")

    def test_miniprogram_without_url_keeps_card_without_guessing_target(self):
        xml = card(33, url=None, extra='<weappinfo><appid>synthetic-app</appid><pagepath>pages/note</pagepath></weappinfo>')
        self.assertEqual(link_metadata((33 << 32) | 49, xml)["url"], None)

    def test_source_display_name_and_embedded_appname(self):
        xml = card(extra="<sourcedisplayname>合成应用</sourcedisplayname>")
        self.assertEqual(link_metadata(49, xml)["source"], "合成应用")
        xml = card(extra="<appinfo><appname>内嵌应用</appname></appinfo>")
        self.assertEqual(link_metadata(49, xml)["source"], "内嵌应用")

    def test_only_explicit_safe_web_targets_are_clickable(self):
        for url in ("javascript:alert(1)", "data:text/html,x", "file:///C:/synthetic.txt", "wx://note",
                    "//example.invalid/path", "https://", "https://user:pass@example.invalid",
                    "https://@example.invalid", "https://example.invalid:bad/", "https://example.invalid:99999/", "https://[invalid]/",
                    "https://example.invalid/line\nfeed", "https://example.invalid/a b",
                    "https://example.invalid\\@other.invalid/", "https://example.invalid/" + "x" * MAX_URL_LENGTH):
            with self.subTest(url=url[:80]):
                metadata = link_metadata(49, card(url=url))
                self.assertIsNotNone(metadata)
                self.assertIsNone(metadata["url"])
        for url in ("https://example.invalid/path#note", "HTTP://example.invalid:8080/path"):
            with self.subTest(url=url):
                self.assertEqual(link_metadata(49, card(url=url))["url"], url)

    def test_quotes_files_music_and_nested_link_cards_stay_their_original_type(self):
        nested = "<refermsg>" + card() + "</refermsg>"
        for subtype in (3, 6, 51, 57, 76, 999):
            for kind in (49, (subtype << 32) | 49):
                with self.subTest(kind=kind):
                    self.assertIsNone(link_metadata(kind, card(subtype, extra=nested)))
        self.assertIsNone(link_metadata(QUOTED_REPLY_TYPE, card(5)))
        self.assertIsNone(link_metadata(1, card()))

    def test_malformed_entity_and_oversized_xml_are_rejected(self):
        for xml in ("plain title", "<msg><appmsg><type>5</type>",
                    '<!DOCTYPE msg [<!ENTITY title "synthetic">]>' + card(title="&title;"),
                    card(title="x" * MAX_XML_LENGTH), card().replace("<type>5</type>", "<type>５</type>"),
                    card().replace("<type>5</type>", "<type>0000000000000005untrusted</type>")):
            with self.subTest(xml=xml[:80]):
                self.assertIsNone(link_metadata(49, xml))

    def test_namespaced_and_invalid_unicode_xml_do_not_abort_message_windows(self):
        for xml in ('<msg xmlns="urn:synthetic"><appmsg><type>5</type></appmsg></msg>',
                    '<s:msg xmlns:s="urn:synthetic"><s:appmsg><s:type>5</s:type></s:appmsg></s:msg>',
                    '<msg><s:appmsg><type>5</type></s:appmsg></msg>',
                    '<msg xmlns:xml="urn:synthetic"><appmsg><type>5</type></appmsg></msg>',
                    card(title="synthetic\x00title"), card(title="synthetic\ud800title")):
            with self.subTest(xml=ascii(xml[:80])):
                self.assertIsNone(link_metadata(49, xml))

    def test_display_fields_are_bounded_without_truncating_url_into_another_target(self):
        metadata = link_metadata(49, card(title="a" * 900, description="b" * 3000,
                                         extra="<sourcedisplayname>" + "c" * 300 + "</sourcedisplayname>"))
        self.assertEqual(len(metadata["title"]), 512)
        self.assertEqual(len(metadata["description"]), 2000)
        self.assertEqual(len(metadata["source"]), 128)


class SourceSharedLinkProjectionTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(account="synthetic-account",
                                  _msg_type_name=lambda kind: "文本" if kind == 1 else "应用消息",
                                  _friendly_content=lambda value, _kind: value.decode())
        self.source = WeChatSource(factory=lambda: self.db, classifier=classify)

    def render(self, content, kind=49, compressed=None, sender=2):
        record = (1, kind, sender, 1700000000, content, compressed, 100, 10)
        return self.source._render_row(self.db, "synthetic-friend",
                                       (record, "message__message_0.db", {1: "synthetic-self", 2: "synthetic-friend"}),
                                       {}, "synthetic-self")

    def test_shared_link_has_metadata_and_keeps_nontext_analysis_kind(self):
        for sender, side in ((1, "self"), (2, "other")):
            message = self.render(card(), sender=sender)
            self.assertEqual(message["kind"], "other")
            self.assertEqual(message["side"], side)
            self.assertEqual(message["text"], "🌿 合成分享：周末散步")
            self.assertEqual(message["link"], link_metadata(49, card()))
            self.assertNotIn("media", message)

    def test_compressed_and_bytes_share_payloads_keep_metadata(self):
        for content, compressed in ((card().encode(), None), ("[应用消息]", card().encode()), (None, card().encode())):
            with self.subTest(content=type(content).__name__):
                self.assertEqual(self.render(content, compressed=compressed)["link"], link_metadata(49, card()))

    def test_share_image_fields_do_not_turn_the_card_into_an_image_message(self):
        message = self.render(card(extra="<imgurl>https://example.invalid/cover.png</imgurl>"))
        self.assertEqual(message["kind"], "other")
        self.assertIsNotNone(message.get("link"))
        self.assertNotIn("media", message)

    def test_plain_messages_and_quoted_links_do_not_become_cards(self):
        message = self.render("看这里 https://example.invalid/note", kind=1)
        self.assertEqual(message["kind"], "text")
        self.assertNotIn("link", message)
        xml = card(57, title="收到", extra="<refermsg>" + card() + "</refermsg>")
        message = self.render(xml, kind=QUOTED_REPLY_TYPE)
        self.assertEqual((message["kind"], message["text"]), ("text", "收到"))
        self.assertNotIn("link", message)


if __name__ == "__main__":
    unittest.main()
