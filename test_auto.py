import asyncio
import importlib
import logging
import shutil
import sys
import types
import unittest
import uuid
from collections import OrderedDict
from pathlib import Path
from unittest.mock import AsyncMock

try:
    import aiohttp
except ModuleNotFoundError:
    module = types.ModuleType("aiohttp")
    module.ClientError = type("ClientError", (Exception,), {})
    sys.modules["aiohttp"] = module

try:
    import astrbot.api
except ModuleNotFoundError:
    for name in [
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.api.message_components",
    ]:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["astrbot.api"].AstrBotConfig = dict
    sys.modules["astrbot.api"].logger = logging.getLogger("meme-auto-test")
    sys.modules["astrbot.api"].logger.addHandler(logging.NullHandler())
    sys.modules["astrbot.api"].logger.propagate = False

    class Plain:
        def __init__(self, text):
            self.text = text

    class Image:
        @classmethod
        def fromFileSystem(cls, path):
            obj = cls()
            obj.file = path
            return obj

    sys.modules["astrbot.api.message_components"].Plain = Plain
    sys.modules["astrbot.api.message_components"].Image = Image
    sys.modules["astrbot.api.event"].AstrMessageEvent = object
    sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL="all"),
        event_message_type=lambda *a, **k: lambda fn: fn,
    )
    sys.modules["astrbot.api.star"].Star = object
    sys.modules["astrbot.api.star"].Context = object
    sys.modules["astrbot.api.star"].register = lambda *a, **k: lambda cls: cls

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pkg = Path(__file__).resolve().parent.name
catalog = importlib.import_module(pkg + ".catalog")
main = importlib.import_module(pkg + ".main")
from astrbot.api.message_components import Plain


class Result:
    def __init__(self, text=None, chain=None):
        self.text, self.chain = text, chain

    def use_t2i(self, value):
        self.t2i = value
        return self


class Event:
    def __init__(self, text="我喜欢初音", group="g", sender="u1", parts=None, fail_send=False):
        self.text, self.group, self.sender = text, group, sender
        self.unified_msg_origin = f"bot:GroupMessage:{group}"
        self.parts = [Plain(text)] if parts is None else parts
        self.sent, self.result, self.llm, self.stopped = [], None, None, False
        self.fail_send = fail_send
        self._has_send_oper = False

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "bot"

    def get_messages(self):
        return self.parts

    def should_call_llm(self, flag):
        self.llm = flag

    def set_result(self, result):
        self.result = result

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return Result(text=text)

    def chain_result(self, chain):
        return Result(chain=chain)

    async def send(self, result):
        self._has_send_oper = True
        if self.fail_send:
            raise RuntimeError("Simulated send failure")
        self.sent.append(result)


class MatchingTests(unittest.TestCase):
    def test_every_enabled_title_and_alias(self):
        for pack in catalog.PACKS:
            for name in catalog.trigger_names(pack):
                with self.subTest(name=name):
                    self.assertEqual(catalog.detect_ip(name, {pack[0]}), pack[0])

    def test_ordinary_sentences_and_full_title(self):
        cases = {
            "我喜欢初音未来": "miku",
            "MyGO真好看": "mygo",
            "我在玩BA呢": "ba",
            "少女乐队的呐喊": "gbc",
            "今天孤独摇滚": "bocchi",
            "ＭＩＫＵ": "miku",
        }
        for text, expected in cases.items():
            self.assertEqual(catalog.detect_ip(text), expected)

    def test_ascii_word_boundaries_and_numeric_alias(self):
        for text in [
            "database",
            "back",
            "basically",
            "mygoplayer",
            "mikumik",
            "0721",
            "20260721",
            "只是普通聊天",
        ]:
            self.assertIsNone(catalog.detect_ip(text), text)

    def test_multiple_ips_first_mention_wins(self):
        self.assertEqual(catalog.detect_ip("MyGO和初音未来"), "mygo")
        self.assertEqual(catalog.detect_ip("初音未来、初音、miku、MyGO"), "miku")

    def test_disabled_categories_never_match(self):
        self.assertIsNone(catalog.detect_ip("初音", {"mygo"}))
        self.assertEqual(catalog.detect_ip("初音和MyGO", {"mygo"}), "mygo")
        self.assertIsNone(catalog.detect_ip("初音", set()))

    def test_fufu_and_miku_are_one_category(self):
        self.assertNotIn("fufu", catalog.BY_KEY)
        for text in ["初音", "FUFU", "miku", "初音未来"]:
            self.assertEqual(catalog.detect_ip(text), "miku")
            self.assertEqual(catalog.parse_command("/随机" + text), ("image", "miku"))

    def test_shigure_names_and_requested_nicknames(self):
        for name in [
            "时雨羽衣",
            "時雨羽衣",
            "羽衣",
            "しぐれうい",
            "Shigure Ui",
            "有妈",
            "妈妈",
            "妈咪",
            "麻麻",
        ]:
            self.assertEqual(catalog.detect_ip("今天" + name + "真可爱"), "shigure_ui")

    def test_pig_migrated_keywords_and_legacy_commands(self):
        for name in ["猪猪", "猪", "祝", "🐷", "🐖", "🐽", "㊗", "㊗️"]:
            self.assertEqual(catalog.detect_ip("今天" + name + "真可爱"), "pig")
        for text in ["/pig", "PIG", "／pig", "/随机猪猪", "/随机表情包 猪猪"]:
            self.assertEqual(catalog.parse_command(text), ("image", "pig"))
        for sub in ["status", "状态"]:
            self.assertEqual(catalog.parse_command("/pig " + sub), ("pig_status", "pig"))
        for sub in ["update", "更新"]:
            self.assertEqual(catalog.parse_command("/pig " + sub), ("pig_update", "pig"))
        self.assertIsNone(catalog.detect_ip("pigment"))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(__file__).resolve().parent / ".test-work"
        self.tmp = self.root / uuid.uuid4().hex
        self.tmp.mkdir(parents=True)
        from PIL import Image as PILImage

        PILImage.new("RGB", (8, 8), "pink").save(self.tmp / "fixture.gif", format="GIF")
        p = object.__new__(main.YuelingRandomMeme)
        p.config = {}
        p.cooldowns, p.auto_busy, p.tasks = OrderedDict(), set(), set()
        p.session = None
        p.index_dir, p.indexes = self.tmp, {}
        p.data, p.last, p.index_lock = self.tmp, OrderedDict(), asyncio.Lock()
        p.prepare = AsyncMock(return_value=self.tmp / "fixture.gif")
        self.plugin = p

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.resolve().relative_to(self.root.resolve())
        shutil.rmtree(self.tmp)

    async def run_event(self, *args, **kwargs):
        event = Event(*args, **kwargs)
        await self.plugin.on_message(event)
        return event

    async def test_group_sentence_without_at_or_command(self):
        event = await self.run_event()
        self.plugin.prepare.assert_awaited_once_with("miku", event.unified_msg_origin)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(event.sent[0].chain), 1)
        self.assertFalse(event.sent[0].t2i)
        self.assertIsNone(event.llm)
        self.assertIsNone(event.result)
        self.assertFalse(event.stopped)
        self.assertFalse(event._has_send_oper)

    async def test_auto_meme_preserves_existing_send_flag_and_failure_state(self):
        for previous, failed in [(False, False), (True, False), (False, True), (True, True)]:
            self.plugin.cooldowns.clear()
            event = Event("妈妈", fail_send=failed)
            event._has_send_oper = previous
            await self.plugin.on_message(event)
            self.assertEqual(event._has_send_oper, previous)
            self.assertIsNone(event.llm)
            self.assertFalse(event.stopped)

    async def test_pig_legacy_command_sends_local_gif(self):
        folder = self.tmp / "local" / "pig"
        folder.mkdir(parents=True)
        shutil.copyfile(self.tmp / "fixture.gif", folder / "pig.gif")
        self.plugin.prepare = types.MethodType(main.YuelingRandomMeme.prepare, self.plugin)
        self.plugin.http = AsyncMock(side_effect=AssertionError("Pig must stay offline"))
        event = await self.run_event("/pig")
        from urllib.parse import urlparse

        location = event.result.chain[-1].file
        self.assertEqual(
            Path(urlparse(location).path if location.startswith("file:") else location),
            folder / "pig.gif",
        )
        self.assertFalse(event.result.t2i)
        self.assertTrue(event.stopped)
        self.plugin.http.assert_not_awaited()

    async def test_pig_status_and_update_scan_without_cooldown_or_network(self):
        folder = self.tmp / "local" / "pig"
        folder.mkdir(parents=True)
        shutil.copyfile(self.tmp / "fixture.gif", folder / "pig.gif")
        for command in ["/pig status", "/pig update"]:
            event = await self.run_event(command)
            self.assertIn("1 张", event.result.text)
            self.assertTrue(event.stopped)
        self.plugin.prepare.assert_not_awaited()
        self.assertEqual(len(self.plugin.cooldowns), 0)

    async def test_pig_disabled_blocks_auto_legacy_and_status(self):
        self.plugin.config["disabled_categories"] = ["pig"]
        event = await self.run_event("小猪真可爱")
        self.assertEqual(event.sent, [])
        for command in ["/pig", "/pig status", "/pig update"]:
            event = await self.run_event(command)
            self.assertIn("未启用", event.result.text)
        self.plugin.prepare.assert_not_awaited()

    async def test_merged_pool_deduplicates_both_sources(self):
        a, b, c = ["https://i0.hdslb.com/bfs/" + n + ".png" for n in "abc"]
        (self.tmp / "初音未来Q.txt").write_text(a + "\n" + b, encoding="utf-8")
        (self.tmp / "fufu.txt").write_text(b + "\n" + c, encoding="utf-8")
        self.assertEqual(await self.plugin.index("miku"), [a, b, c])
        self.assertEqual(len(self.plugin.indexes["miku"]), 3)
        self.assertIn("3 条索引", self.plugin.menu())

    async def test_partial_pool_works_and_retries_missing_half(self):
        self.plugin.load_index_file = AsyncMock(
            side_effect=[["a"], ValueError("missing"), ["a"], ["b"]]
        )
        self.assertEqual(await self.plugin.index("miku"), ["a"])
        self.assertNotIn("miku", self.plugin.indexes)
        self.assertEqual(await self.plugin.index("miku"), ["a", "b"])

    async def test_shigure_local_images_require_no_network_and_skip_broken(self):
        folder = self.tmp / "local" / "shigure_ui"
        folder.mkdir(parents=True)
        shutil.copyfile(self.tmp / "fixture.gif", folder / "good.gif")
        (folder / "broken.gif").write_bytes(b"broken")
        self.plugin.http = AsyncMock(side_effect=AssertionError("No network for Shigure"))
        self.assertEqual(
            await main.YuelingRandomMeme.prepare(self.plugin, "shigure_ui", "g"),
            folder / "good.gif",
        )
        self.plugin.http.assert_not_awaited()

    async def test_upload_to_online_category_works_immediately_without_network(self):
        folder = self.tmp / "local" / "mygo"
        folder.mkdir(parents=True)
        shutil.copyfile(self.tmp / "fixture.gif", folder / "uploaded.gif")
        self.plugin.http = AsyncMock(side_effect=AssertionError("Use uploaded local image"))
        self.assertEqual(
            await main.YuelingRandomMeme.prepare(self.plugin, "mygo", "g"), folder / "uploaded.gif"
        )
        self.assertIn("1 张本地表情", self.plugin.menu())
        self.plugin.http.assert_not_awaited()

    async def test_merged_list_and_shigure_plain_message(self):
        listing = self.plugin.ip_list()
        self.assertIn("初音未来 / Fufu", listing)
        self.assertEqual(listing.count("Fufu"), 1)
        self.assertIn("时雨羽衣", listing)
        event = await self.run_event("妈咪")
        self.plugin.prepare.assert_awaited_once_with("shigure_ui", event.unified_msg_origin)
        self.assertEqual(len(event.sent), 1)

    async def test_old_disabled_names_disable_merged_category(self):
        for name in ["初音未来", "fufu", "miku"]:
            self.plugin.config = {"disabled_categories": [name]}
            self.assertNotIn("miku", {p[0] for p in self.plugin.enabled_packs()})

    async def test_multiple_matches_still_only_one_image(self):
        event = await self.run_event("初音未来 miku MyGO")
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.plugin.prepare.await_count, 1)

    async def test_cooldown_is_silent_and_scoped_per_user_per_group(self):
        self.assertEqual(len((await self.run_event()).sent), 1)
        cooled = await self.run_event()
        self.assertEqual(cooled.sent, [])
        self.assertIsNone(cooled.result)
        self.assertFalse(cooled.stopped)
        self.assertEqual(len((await self.run_event(sender="u2")).sent), 1)
        self.assertEqual(len((await self.run_event(group="g2")).sent), 1)

    async def test_private_ip_not_automatic_but_manual_commands_work(self):
        self.assertEqual((await self.run_event(group="")).sent, [])
        event = await self.run_event("/随机初音", group="")
        self.assertIsNotNone(event.result.chain)
        self.assertTrue(event.stopped)

    async def test_group_manual_command_no_at_required(self):
        event = await self.run_event("/随机mygo")
        self.assertIsNotNone(event.result.chain)
        self.assertEqual(event.sent, [])
        self.assertFalse(event.llm)

    async def test_menu_and_ip_list_no_at_and_no_download(self):
        for command in ["/表情包IP列表", "/随机表情包"]:
            event = await self.run_event(command)
            self.assertIn("无需", event.result.text)
            self.assertTrue(event.stopped)
        self.plugin.prepare.assert_not_awaited()

    async def test_other_plugin_commands_are_left_alone(self):
        for text in ["/生图 初音", "／生图 MyGO", "普通聊天"]:
            event = await self.run_event(text)
            self.assertEqual(event.sent, [])
            self.assertIsNone(event.llm)
            self.assertFalse(event.stopped)

    async def test_only_current_plain_text_not_reply_or_image_text(self):
        event = await self.run_event(parts=[types.SimpleNamespace(text="初音"), Plain("同意")])
        self.assertEqual(event.sent, [])
        event = await self.run_event(parts=[object(), Plain("MyGO"), object()])
        self.assertEqual(len(event.sent), 1)

    async def test_disabled_category_switch_and_self_messages(self):
        self.plugin.config["disabled_categories"] = ["初音未来"]
        self.assertEqual((await self.run_event()).sent, [])
        self.plugin.config = {"auto_trigger_enabled": False}
        self.assertEqual((await self.run_event()).sent, [])
        self.assertIsNotNone((await self.run_event("/随机mygo")).result.chain)
        self.plugin.config = {"enabled": False}
        self.assertIsNone((await self.run_event("/随机mygo")).result)
        self.plugin.config = {}
        self.assertEqual((await self.run_event(sender="bot")).sent, [])

    async def test_source_failure_is_silent_and_releases_group(self):
        self.plugin.prepare.side_effect = RuntimeError("Simulated source failure")
        event = await self.run_event()
        self.assertEqual(event.sent, [])
        self.assertIsNone(event.result)
        self.assertFalse(event.stopped)
        self.assertEqual(self.plugin.auto_busy, set())
        self.assertEqual(self.plugin.tasks, set())

    async def test_inflight_group_does_not_queue_multiple_downloads(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def prepare(*args):
            started.set()
            await finish.wait()
            return self.tmp / "fixture.gif"

        self.plugin.prepare = AsyncMock(side_effect=prepare)
        first = Event()
        task = asyncio.create_task(self.plugin.on_message(first))
        await started.wait()
        second = await self.run_event(sender="u2")
        self.assertEqual(second.sent, [])
        finish.set()
        await task
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(self.plugin.prepare.await_count, 1)

    async def test_terminate_cancels_pending_fetch(self):
        started = asyncio.Event()

        async def prepare(*args):
            started.set()
            await asyncio.Event().wait()

        self.plugin.prepare = AsyncMock(side_effect=prepare)
        task = asyncio.create_task(self.plugin.on_message(Event()))
        await started.wait()
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plugin.auto_busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
