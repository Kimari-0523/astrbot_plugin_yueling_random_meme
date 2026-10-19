import importlib
import json
import shutil
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_auto as support
from test_upload import workspace_temp

module = importlib.import_module(Path(__file__).parent.name + ".categories")


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = workspace_temp()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "categories.json"
        self.store = module.CategoryStore(self.path)

    def test_creation_persists_after_reload(self):
        row = self.store.create("作品甲", "别名甲，别名乙\n昵称丙")
        self.assertTrue(row[0].startswith("custom_"))
        loaded = module.CategoryStore(self.path)
        self.assertEqual(loaded.custom, self.store.custom)
        self.assertEqual(len(loaded.packs()), len(support.catalog.PACKS) + 1)

    def test_conflicting_builtin_or_custom_names_rejected(self):
        for word in ["初音", "Ｆｕｆｕ", "MyGO", "分类表情包"]:
            with self.assertRaises(ValueError):
                self.store.create(word, [])
        self.store.create("作品甲", ["昵称甲"])
        for title, aliases in [("作品甲", []), ("作品乙", ["昵称甲"]), ("作品乙", ["miku"])]:
            with self.assertRaises(ValueError):
                self.store.create(title, aliases)
        self.assertEqual(len(self.store.custom), 1)

    def test_invalid_names_and_aliases_never_write_file(self):
        for title, aliases in [
            ("", []),
            ("../逃逸", []),
            ("1234", []),
            ("a" * 33, []),
            ("作品", ["b" * 41]),
            ("作品", 42),
            ("作品", ["a"] * 65),
            ("作品", [f"别名{i}" for i in range(64)]),
            ("作品\x00", []),
        ]:
            with self.assertRaises(ValueError):
                self.store.create(title, aliases)
        self.assertFalse(self.path.exists())

    def test_duplicate_names_within_category_deduplicated(self):
        row = self.store.create("作品", ["作品", "Alias", "ＡＬＩＡＳ"])
        self.assertEqual(row[3], ["作品", "Alias"])

    def test_merged_ai_aliases_remain_valid_after_reload(self):
        aliases = [
            "AI",
            "Claude",
            "克劳德",
            "Gemini",
            "DeepSeek",
            "深度求索",
            "豆包",
            "Doubao",
            "GPT",
            "ChatGPT",
            "OpenAI",
            "千问",
            "Qwen",
            "通义千问",
            "GLM",
            "ChatGLM",
            "智谱",
            "智谱清言",
            "Kimi",
            "月之暗面",
            "Grok",
            "格罗克",
            "xAI",
            "人工智能",
        ]
        row = self.store.create("AI", aliases)
        loaded = module.CategoryStore(self.path)
        self.assertEqual(loaded.custom, self.store.custom)
        patterns = support.catalog.build_patterns(loaded.packs())
        for word in aliases:
            self.assertEqual(support.catalog.detect_ip("我喜欢" + word, patterns=patterns), row[0])

    def test_create_cap_and_invalid_persistence_preserved(self):
        self.store.custom = [
            ("custom_" + str(i).zfill(12), "作品" + str(i), None, []) for i in range(100)
        ]
        with self.assertRaises(ValueError):
            self.store.create("新作品", [])
        raw = '{"version": 1, "categories": [{"key": "../escape"}]}'
        self.path.write_text(raw)
        with self.assertRaises(ValueError):
            module.CategoryStore(self.path)
        self.assertEqual(self.path.read_text(), raw)


class DynamicHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = support.HandlerTests()
        await self.fixture.asyncSetUp()
        self.plugin = self.fixture.plugin
        self.plugin.category_store = module.CategoryStore(self.fixture.tmp / "categories.json")
        self.row = self.plugin.category_store.create("示例新作品", "别名甲,Sample Work")
        self.plugin.refresh_catalog()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_new_name_triggers_and_lists_immediately(self):
        key = self.row[0]
        self.assertIn("示例新作品", self.plugin.ip_list())
        self.assertIn("示例新作品", self.plugin.menu((len(self.plugin.all_packs()) + 7) // 8))
        self.assertNotIn(key, self.plugin.ip_list())
        event = await self.fixture.run_event("我喜欢别名甲")
        self.plugin.prepare.assert_awaited_once_with(key, event.unified_msg_origin)
        self.assertEqual(len(event.sent), 1)

    async def test_custom_upload_used_immediately_and_multiword_manual_command(self):
        key = self.row[0]
        folder = self.plugin.data / "local" / key
        folder.mkdir(parents=True)
        shutil.copyfile(self.fixture.tmp / "fixture.gif", folder / "uploaded.gif")
        self.plugin.prepare = types.MethodType(support.main.YuelingRandomMeme.prepare, self.plugin)
        self.plugin.http = AsyncMock(side_effect=AssertionError("No network for custom category"))
        event = await self.fixture.run_event("/随机Sample Work")
        self.assertIsNotNone(event.result.chain)
        self.assertIn("示例新作品", event.result.chain[0].text)
        self.plugin.http.assert_not_awaited()

    async def test_empty_custom_has_no_network_fallback_and_disabled_alias_ignored(self):
        key = self.row[0]
        with self.assertRaises(RuntimeError):
            await support.main.YuelingRandomMeme.prepare(self.plugin, key, "g")
        empty = await self.fixture.run_event("/随机示例新作品")
        self.assertIn("先在后台上传", empty.result.text)
        self.plugin.config = {"disabled_categories": ["示例新作品"]}
        event = await self.fixture.run_event("别名甲")
        self.assertEqual(event.sent, [])

    async def test_online_count_is_distinct_from_local_count(self):
        (self.fixture.tmp / "mygo.txt").write_text(
            "https://i0.hdslb.com/bfs/one.png\nhttps://i0.hdslb.com/bfs/two.png"
        )
        self.assertEqual(self.plugin.online_count("mygo"), 2)
        self.assertEqual(len(self.plugin.local_images("mygo")), 0)
        self.assertEqual(self.plugin.online_count(self.row[0]), 0)
        self.assertIsNone(self.plugin.online_count("miku"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
