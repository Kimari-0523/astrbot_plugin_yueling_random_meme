import asyncio
import importlib
import io
import json
import shutil
import sys
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

import test_auto as support

pkg = Path(__file__).parent.name
gallery = importlib.import_module(pkg + ".gallery")
try:
    import astrbot.api.web
except ModuleNotFoundError:
    shim = types.ModuleType("astrbot.api.web")
    shim.PluginUploadFile = object
    shim.request = None
    shim.json_response = lambda data: data
    shim.error_response = lambda message, status_code=400: {
        "status": "error",
        "message": message,
        "code": status_code,
    }
    sys.modules["astrbot.api.web"] = shim
web = importlib.import_module(pkg + ".upload_web")


def picture(fmt="PNG", animated=False):
    output = io.BytesIO()
    frames = [Image.new("RGB", (16, 16), color) for color in ("pink", "green", "blue")]
    if animated:
        frames[0].save(
            output,
            format=fmt,
            save_all=True,
            append_images=frames[1:],
            duration=[50, 90, 120],
            loop=0,
        )
    else:
        frames[0].save(output, format=fmt)
    return output.getvalue()


def workspace_temp():
    root = Path(__file__).resolve().parent / ".test-work"
    root.mkdir(exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir()

    def cleanup():
        path.resolve().relative_to(root.resolve())
        shutil.rmtree(path)

    return types.SimpleNamespace(name=str(path), cleanup=cleanup)


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp = workspace_temp()
        self.addCleanup(self.temp.cleanup)
        self.store = gallery.LocalGallery(Path(self.temp.name) / "local", ["miku", "shigure_ui"])

    def test_import_all_supported_formats_and_sniff_real_format(self):
        for fmt in ["PNG", "JPEG", "GIF", "WEBP"]:
            result = self.store.import_image("miku", picture(fmt))
            file = self.store.folder("miku") / result["filename"]
            self.assertTrue(file.exists())
            self.assertNotEqual(file.suffix, ".webp")
            with Image.open(file) as image:
                image.load()

    def test_duplicate_does_not_write_or_change_count(self):
        raw = picture()
        first = self.store.import_image("miku", raw)
        p = self.store.folder("miku") / first["filename"]
        before = p.stat().st_mtime_ns
        self.assertTrue(self.store.import_image("miku", raw)["duplicate"])
        self.assertEqual(p.stat().st_mtime_ns, before)
        self.assertEqual(self.store.stats()["miku"]["count"], 1)
        self.assertTrue(self.store.import_image("shigure_ui", raw)["added"])

    def test_gif_preserves_original_bytes_and_frames(self):
        raw = picture("GIF", True)
        result = self.store.import_image("miku", raw)
        self.assertEqual((self.store.folder("miku") / result["filename"]).read_bytes(), raw)
        self.assertEqual(result["frames"], 3)

    def test_animated_webp_becomes_animated_gif(self):
        result = self.store.import_image("miku", picture("WEBP", True))
        self.assertTrue(result["filename"].endswith(".gif"))
        with Image.open(self.store.folder("miku") / result["filename"]) as image:
            self.assertEqual(image.n_frames, 3)
            for i in range(image.n_frames):
                image.seek(i)
                image.load()

    def test_rejects_corrupt_truncated_and_nonimage(self):
        for raw in [
            b"",
            b"<script>alert(1)</script>",
            b"GIF89a",
            picture("JPEG")[:60],
            picture("GIF", True)[:40],
        ]:
            with self.assertRaises(ValueError):
                self.store.import_image("miku", raw)
        self.assertEqual(self.store.stats()["miku"]["count"], 0)

    def test_file_size_limit(self):
        with self.assertRaises(ValueError):
            gallery.normalize_image(b"x" * (gallery.MAX_FILE_BYTES + 1))

    def test_dimensions_and_frame_budget_limits(self):
        with patch.object(gallery, "MAX_FRAME_PIXELS", 1):
            with self.assertRaises(ValueError):
                gallery.normalize_image(picture())
        with patch.object(gallery, "MAX_FRAMES", 2):
            with self.assertRaises(ValueError):
                gallery.normalize_image(picture("GIF", True))

    def test_category_path_cannot_escape(self):
        for key in ["../outside", "/tmp/escape", "miku/../shigure_ui", "unknown", "fufu"]:
            with self.assertRaises(ValueError):
                self.store.import_image(key, picture())

    def test_library_budget_and_duplicates_at_limit(self):
        self.store.import_image("miku", picture())
        with patch.object(gallery, "MAX_LIBRARY_BYTES", 1):
            self.assertTrue(self.store.import_image("miku", picture())["duplicate"])
            with self.assertRaises(ValueError):
                self.store.import_image("shigure_ui", picture())

    def test_concurrent_duplicate_upload_is_one_file(self):
        raw = picture()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.import_image("miku", raw), range(8)))
        self.assertEqual(sum(r["added"] for r in results), 1)
        self.assertEqual(len(list(self.store.folder("miku").glob("*.pending"))), 0)

    def test_files_persist_in_new_store(self):
        self.store.import_image("miku", picture())
        new = gallery.LocalGallery(self.store.root, self.store.keys)
        self.assertEqual(new.stats()["miku"]["count"], 1)


class Context:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, *row):
        self.registered_web_apis[:] = [
            r for r in self.registered_web_apis if (r[0], r[2]) != (row[0], row[2])
        ]
        self.registered_web_apis.append(row)


class Upload:
    def __init__(self, raw):
        self.raw = raw
        self.filename = "../../untrusted.png"

    async def read(self, size):
        return self.raw[:size]


def payload(response):
    return json.loads(response.body) if hasattr(response, "body") else response


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = workspace_temp()
        self.addCleanup(self.temp.cleanup)
        self.plugin = types.SimpleNamespace(
            data=Path(self.temp.name), config={}, enabled_packs=lambda: support.catalog.PACKS
        )
        self.context = Context()
        self.controller = web.GalleryWeb(self.plugin, self.context)
        self.request = types.SimpleNamespace(
            username="admin",
            headers={},
            files=AsyncMock(return_value={"file": Upload(picture())}),
            json=AsyncMock(return_value={"title": "示例作品", "aliases": "昵称甲，昵称乙"}),
        )
        self.patcher = patch.multiple(web, request=self.request, PluginUploadFile=Upload)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_upload_and_duplicate_and_status(self):
        self.assertTrue(payload(await self.controller.upload("miku"))["added"])
        self.assertTrue(payload(await self.controller.upload("miku"))["duplicate"])
        result = payload(await self.controller.status())
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(next(x for x in result["categories"] if x["key"] == "miku")["count"], 1)

    async def test_unauthenticated_denied_before_parsing(self):
        self.request.username = None
        self.assertEqual(payload(await self.controller.upload("miku"))["status"], "error")
        self.assertEqual(payload(await self.controller.status())["status"], "error")
        self.request.files.assert_not_awaited()

    async def test_oversized_header_denied_before_parsing(self):
        self.request.headers = {"content-length": str(9 * 1024 * 1024)}
        self.assertEqual(payload(await self.controller.upload("miku"))["status"], "error")
        self.request.files.assert_not_awaited()

    async def test_unknown_category_denied_before_parsing(self):
        self.assertEqual(payload(await self.controller.upload("../escape"))["status"], "error")
        self.request.files.assert_not_awaited()

    async def test_missing_file_and_corruption_have_clear_errors(self):
        self.request.files.return_value = {}
        self.assertEqual(payload(await self.controller.upload("miku"))["status"], "error")
        self.request.files.return_value = {"file": Upload(b"bad")}
        self.assertEqual(payload(await self.controller.upload("miku"))["status"], "error")

    async def test_unload_removes_only_own_api_handlers(self):
        self.context.register_web_api("/other", lambda: None, ["GET"], "other")
        self.controller.close()
        self.assertEqual(len(self.context.registered_web_apis), 1)
        self.assertEqual(self.context.registered_web_apis[0][0], "/other")

    async def test_new_controller_replaces_routes_after_reload(self):
        new = web.GalleryWeb(self.plugin, self.context)
        self.controller.close()
        self.assertEqual(len(self.context.registered_web_apis), 8)
        new.close()
        self.assertEqual(self.context.registered_web_apis, [])

    async def test_create_category_and_upload_immediately(self):
        result = payload(await self.controller.create_category())
        self.assertEqual(result["title"], "示例作品")
        self.assertTrue(payload(await self.controller.upload(result["key"]))["added"])
        self.assertEqual(payload(await self.controller.status())["total_count"], 1)
        again = payload(await self.controller.create_category())
        self.assertEqual(again["status"], "error")

    async def test_create_category_auth_and_bad_payload(self):
        self.request.username = None
        self.assertEqual(payload(await self.controller.create_category())["status"], "error")
        self.request.json.assert_not_awaited()
        self.request.username = "admin"
        self.request.json.return_value = ["wrong"]
        self.assertEqual(payload(await self.controller.create_category())["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
