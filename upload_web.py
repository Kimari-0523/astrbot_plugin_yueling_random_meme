import asyncio
import json

from astrbot.api.web import PluginUploadFile, error_response, json_response, request

from .capture import ReviewStore
from .catalog import trigger_names
from .categories import CategoryStore
from .gallery import MAX_FILE_BYTES, MAX_LIBRARY_BYTES, LocalGallery

PLUGIN_NAME = "astrbot_plugin_yueling_random_meme"


class GalleryWeb:
    def __init__(self, plugin, context):
        self.plugin, self.context = plugin, context
        self.store = getattr(plugin, "category_store", None) or CategoryStore(
            plugin.data / "categories.json"
        )
        self.gallery = LocalGallery(plugin.data / "local", [p[0] for p in self.store.packs()])
        self.slot = asyncio.Semaphore(1)
        self.review_store = ReviewStore(plugin.data / "review")
        context.register_web_api(
            f"/{PLUGIN_NAME}/gallery", self.status, ["GET"], "查看本地表情分类与数量"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/upload/<key>", self.upload, ["POST"], "向指定分类上传一张图片"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/categories", self.create_category, ["POST"], "新增本地表情分类与触发词"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/review", self.review_list, ["GET"], "查看待审核群聊表情"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/review/page/<page>",
            self.review_list,
            ["GET"],
            "分页查看待审核群聊表情",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/review/<identifier>/image",
            self.review_image,
            ["GET"],
            "查看待审核原图或动图",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/review/<identifier>/approve",
            self.review_approve,
            ["POST"],
            "审核表情并添加到指定分类",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/review/<identifier>/reject",
            self.review_reject,
            ["POST"],
            "忽略此表情并避免重复收集",
        )

    async def review_list(self, page=1):
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        try:
            result = await asyncio.to_thread(self.review_store.rows, page)
            config = self.plugin.config
            result["settings"] = {
                "enabled": config.get("enabled", True) and config.get("collect_enabled", True),
                "probability": config.get("collect_probability", 20),
                "cooldown": config.get("collect_cooldown_seconds", 120),
                "vision": config.get("collect_vision_enabled", True),
            }
            diagnostic = self.review_store.root / "last-attempt.json"
            result["last_attempt"] = (
                json.loads(diagnostic.read_text(encoding="utf-8")) if diagnostic.exists() else None
            )
            return json_response(result)
        except (ValueError, OSError):
            return error_response("待审核列表读取失败", status_code=400)

    async def review_image(self, identifier):
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        try:
            return json_response(
                {"data_url": await asyncio.to_thread(self.review_store.image, identifier)}
            )
        except (ValueError, OSError):
            return error_response("此图片不存在或已经审核", status_code=404)

    async def review_approve(self, identifier):
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict) or not isinstance(payload.get("category"), str):
                raise ValueError("请选择分类")
            async with self.slot:
                self.gallery.keys = {p[0] for p in self.store.packs()}
                result = await asyncio.to_thread(
                    self.review_store.approve, identifier, payload["category"], self.gallery
                )
            return json_response(result)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("入库失败，请稍后重试", status_code=500)

    async def review_reject(self, identifier):
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        try:
            async with self.slot:
                result = await asyncio.to_thread(self.review_store.reject, identifier)
            return json_response(result)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("忽略失败，请稍后重试", status_code=500)

    async def status(self):
        # Dashboard authentication and plugin scope are also enforced by AstrBot.
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        packs = self.store.packs()
        self.gallery.keys = {p[0] for p in packs}
        stats = await asyncio.to_thread(self.gallery.stats)
        enabled = {p[0] for p in self.plugin.enabled_packs()}
        counts = await asyncio.to_thread(
            lambda: {
                p[0]: self.plugin.online_count(p[0]) if hasattr(self.plugin, "online_count") else 0
                for p in packs
            }
        )
        rows = [
            {
                "key": p[0],
                "title": p[1],
                "aliases": trigger_names(p),
                "enabled": p[0] in enabled,
                "online_count": counts[p[0]],
                "custom": p[0].startswith("custom_"),
                **stats[p[0]],
            }
            for p in packs
        ]
        return json_response(
            {
                "categories": rows,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_library_bytes": MAX_LIBRARY_BYTES,
                "total_bytes": sum(x["bytes"] for x in stats.values()),
                "total_count": sum(x["count"] for x in stats.values()),
            }
        )

    async def create_category(self):
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        try:
            length = request.headers.get("content-length")
            if length and int(length) > 16 * 1024:
                return error_response("分类信息过长", status_code=413)
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请填写分类名称和触发词", status_code=400)
            async with self.slot:
                row = await asyncio.to_thread(
                    self.store.create, payload.get("title"), payload.get("aliases", [])
                )
                self.gallery.keys = {p[0] for p in self.store.packs()}
                if hasattr(self.plugin, "refresh_catalog"):
                    self.plugin.refresh_catalog()
            return json_response({"key": row[0], "title": row[1], "aliases": trigger_names(row)})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("分类保存失败，请检查磁盘空间", status_code=500)

    async def upload(self, key):
        if not request.username:
            return error_response("请先登录 AstrBot 后台", status_code=403)
        try:
            self.gallery.keys = {p[0] for p in self.store.packs()}
            self.gallery.folder(key)
            length = request.headers.get("content-length")
            if length and int(length) > MAX_FILE_BYTES + 64 * 1024:
                return error_response("每张图片不能超过 8 MiB", status_code=413)
            async with self.slot:
                files = await request.files()
                image = files.get("file")
                if not isinstance(image, PluginUploadFile):
                    return error_response("请选择一张图片", status_code=400)
                raw = await image.read(MAX_FILE_BYTES + 1)
                result = await asyncio.to_thread(self.gallery.import_image, key, raw)
            result["category"] = key
            return json_response(result)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("本地图库暂时无法写入，请检查磁盘空间", status_code=500)

    def close(self):
        self.context.registered_web_apis[:] = [
            row
            for row in self.context.registered_web_apis
            if getattr(row[1], "__self__", None) is not self
        ]
