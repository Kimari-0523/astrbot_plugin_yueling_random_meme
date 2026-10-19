import asyncio
import base64
import hashlib
import io
import math
import random
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from PIL import Image as PILImage

from .catalog import INDEX_SOURCES, PACKS, build_patterns, detect_ip, parse_command, trigger_names
from .categories import CategoryStore

INDEX_API = "https://api.github.com/repos/koishi-shangxue-plugins/koishi-shangxue-apps/contents/plugins/emojihub-bili/txts/"


def image_url(line):
    value = line.strip().split("?", 1)[0]
    if not value or value.startswith("#"):
        return None
    if not value.startswith(("https://", "http://")):
        value = "https://i0.hdslb.com/bfs/" + value.lstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    # Only use the known public image hosts in this curated index.
    if parts.hostname not in {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}:
        return None
    return urlunsplit(("https", parts.netloc, parts.path, "", ""))


def inspect_image(data):
    with PILImage.open(io.BytesIO(data)) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("Image too large")
        fmt = image.format
        image.verify()
    if fmt not in {"JPEG", "PNG", "GIF", "WEBP"}:
        raise ValueError("Unsupported image")
    # Fully decode the first frame, catching incomplete downloads as well.
    with PILImage.open(io.BytesIO(data)) as image:
        image.load()
        if fmt == "WEBP":
            # QQ Official image upload accepts GIF/JPEG/PNG; preserve WebP animation.
            output = io.BytesIO()
            if getattr(image, "is_animated", False):
                from PIL import ImageSequence

                frames = []
                pixels = 0
                for frame in ImageSequence.Iterator(image):
                    pixels += frame.width * frame.height
                    if len(frames) >= 100 or pixels > 8_000_000:
                        raise ValueError("Animation too large")
                    frames.append(frame.convert("RGBA"))
                frames[0].save(
                    output,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    loop=0,
                    duration=image.info.get("duration", 100),
                    disposal=2,
                )
                return output.getvalue(), ".gif"
            image.convert("RGBA").save(output, format="PNG")
            return output.getvalue(), ".png"
    return data, {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}[fmt]


@register(
    "yueling_random_meme",
    "Kimari-0523",
    "随机偷表情待审核入库；提到IP自动发图，不拦截@聊天",
    "1.7.0",
)
class YuelingRandomMeme(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data = (
            Path(__file__).resolve().parents[2]
            / "plugin_data"
            / "astrbot_plugin_yueling_random_meme"
        )
        self.index_dir = self.data / "indexes"
        self.cache = self.data / "cache"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        # Import bundled, user-supplied stickers into persistent storage once.
        bundled = Path(__file__).resolve().parent / "assets" / "shigure_ui"
        target = self.data / "local" / "shigure_ui"
        target.mkdir(parents=True, exist_ok=True)
        for source in bundled.glob("*"):
            if (
                source.is_file()
                and not source.is_symlink()
                and source.suffix.lower() in {".gif", ".png", ".jpg"}
            ):
                dest = target / source.name
                if not dest.exists():
                    temp = dest.with_suffix(dest.suffix + ".pending")
                    temp.write_bytes(source.read_bytes())
                    temp.replace(dest)
        self.indexes = {}
        self.index_lock = asyncio.Lock()
        self.download_slots = asyncio.Semaphore(2)
        self.last = OrderedDict()
        self.cooldowns = OrderedDict()
        self.bad_until = {}
        self.session = None
        self.tasks = set()
        self.auto_busy = set()
        self.category_store = CategoryStore(self.data / "categories.json")
        self.refresh_catalog()
        from .upload_web import GalleryWeb

        self.web_gallery = GalleryWeb(self, context)
        from .capture import StickerCollector

        self.collector = StickerCollector(
            self, context, self.web_gallery.review_store, self.web_gallery.gallery
        )
        logger.info("[柚铃随机表情包] loaded v1.7.0: 随机偷表情需后台审核分类后入库；保留正常@聊天")

    def all_packs(self):
        store = getattr(self, "category_store", None)
        return store.packs() if store else PACKS

    def refresh_catalog(self):
        packs = self.all_packs()
        self.category_aliases = {
            name.casefold(): p[0] for p in packs for name in [p[0], p[1], *p[3]]
        }
        self.category_patterns = build_patterns(packs)

    def category_title(self, key):
        return next(p[1] for p in self.all_packs() if p[0] == key)

    def online_count(self, key):
        if key not in INDEX_SOURCES:
            return 0
        if key in self.indexes:
            return len(self.indexes[key])
        urls, found = set(), False
        for source in INDEX_SOURCES[key]:
            path = self.index_dir / source
            if path.exists():
                found = True
                urls.update(
                    u
                    for line in path.read_text(encoding="utf-8-sig").splitlines()
                    if (u := image_url(line))
                )
        return len(urls) if found else None

    def enabled_packs(self):
        disabled = {str(x).casefold() for x in self.config.get("disabled_categories", [])}
        # Preserve old category exclusions after combining Miku and Fufu.
        if disabled.intersection({"fufu", "初音未来"}):
            disabled.add("miku")
        return [
            p for p in self.all_packs() if p[0] not in disabled and p[1].casefold() not in disabled
        ]

    def local_images(self, key):
        folder = self.data / "local" / key
        return sorted(
            p
            for p in folder.glob("*")
            if p.is_file()
            and not p.is_symlink()
            and p.suffix.lower() in {".gif", ".png", ".jpg", ".jpeg"}
        )

    def menu(self, page=1):
        packs = self.enabled_packs()
        pages = max(1, math.ceil(len(packs) / 8))
        if page < 1 or page > pages:
            return f"页码范围：1—{pages}。例如：/随机表情包 1"
        lines = [
            f"柚铃随机表情包（{page}/{pages}）",
            "群聊提到IP名称或别名就自动发一张，无需@或指令。也可手动抽图：",
        ]
        for key, title, filename, aliases in packs[(page - 1) * 8 : page * 8]:
            count = len(self.indexes.get(key, []))
            local_count = len(self.local_images(key))
            if local_count or key not in INDEX_SOURCES:
                count = local_count
            elif not count:
                urls = set()
                for source in INDEX_SOURCES.get(key, ()):
                    path = self.index_dir / source
                    if path.exists():
                        urls.update(
                            u
                            for line in path.read_text(encoding="utf-8-sig").splitlines()
                            if (u := image_url(line))
                        )
                count = len(urls)
            unit = "张本地表情" if local_count or key not in INDEX_SOURCES else "条索引"
            lines.append(f"/随机{aliases[0]} — {title}" + (f"（{count} {unit}）" if count else ""))
        lines.extend(
            [
                "/表情包IP列表：一条消息查看全部作品和角色分类",
                "/随机分类表情包：随机选一类，再抽一张",
                "/随机表情包 初音：指定分类抽图",
                "初音别名：/随机初音未来、/随机miku",
                f"翻页：/随机表情包 {page % pages + 1}",
            ]
        )
        return "\n".join(lines)

    def ip_list(self):
        packs = self.enabled_packs()
        if not packs:
            return "当前暂无启用的表情包作品或角色分类。"
        lines = [
            f"【表情包 IP / 作品与角色分类】共 {len(packs)} 类",
            "群里直接提到以下名称或别名，就自动发一张；无需@或输入指令：",
        ]
        lines.extend(f"{i}. " + " / ".join(trigger_names(pack)) for i, pack in enumerate(packs, 1))
        lines.append("一句多个IP只发一张，默认每人冷却5秒；手动菜单：/随机表情包（无需@）")
        return "\n".join(lines)

    async def http(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=4),
                headers={
                    "User-Agent": "YuelingRandomMeme/1.0 (+https://github.com/Kimari-0523/astrbot_plugin_yueling_random_meme)",
                    "Referer": "https://www.bilibili.com/",
                },
            )
        return self.session

    async def index(self, key):
        if key in self.indexes:
            return self.indexes[key]
        async with self.index_lock:
            if key in self.indexes:
                return self.indexes[key]
            combined, failed = [], False
            for filename in INDEX_SOURCES[key]:
                try:
                    combined.extend(await self.load_index_file(filename))
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                    failed = True
                    logger.warning("[柚铃随机表情包] 索引暂不可用: %s", filename)
            urls = list(dict.fromkeys(combined))
            if not urls:
                raise ValueError("Index is empty")
            # Retry a missing half on the next request instead of caching a partial pool.
            if not failed:
                self.indexes[key] = urls
            return urls

    async def load_index_file(self, filename):
        path = self.index_dir / filename
        if path.exists():
            text = path.read_text(encoding="utf-8-sig")
        else:
            session = await self.http()
            async with session.get(INDEX_API + quote(filename), allow_redirects=False) as response:
                response.raise_for_status()
                raw = await self.read_limited(response, 2 * 1024 * 1024)
                import json

                document = json.loads(raw)
                text = base64.b64decode(document["content"], validate=False).decode("utf-8-sig")
            urls = [u for line in text.splitlines() if (u := image_url(line))]
            if not urls:
                raise ValueError("Index has no supported images")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        urls = list(dict.fromkeys(u for line in text.splitlines() if (u := image_url(line))))
        if not urls:
            raise ValueError("Index is empty")
        return urls

    @staticmethod
    async def read_limited(response, limit):
        raw = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError("Response exceeds size limit")
        return bytes(raw)

    async def prepare(self, key, origin):
        files = self.local_images(key)
        if files or key not in INDEX_SOURCES:
            previous = self.last.get((origin, key))
            choices = [p for p in files if str(p) != previous]
            random.shuffle(choices)
            choices += [p for p in files if str(p) == previous]
            for path in choices:
                try:
                    if path.stat().st_size > 8 * 1024 * 1024:
                        continue
                    await asyncio.to_thread(inspect_image, path.read_bytes())
                    self.last[(origin, key)] = str(path)
                    self.last.move_to_end((origin, key))
                    while len(self.last) > 1024:
                        self.last.popitem(last=False)
                    return path
                except (OSError, ValueError, PILImage.DecompressionBombError):
                    continue
            if key not in INDEX_SOURCES:
                raise RuntimeError("No usable local image")
        urls = await self.index(key)
        async with self.download_slots:
            previous = self.last.get((origin, key))
            choices = [
                u for u in urls if u != previous and self.bad_until.get(u, 0) < time.monotonic()
            ]
            if not choices:
                choices = [u for u in urls if self.bad_until.get(u, 0) < time.monotonic()]
            random.shuffle(choices)
            error = None
            for url in choices[:3]:
                digest = hashlib.sha256(url.encode()).hexdigest()
                cached = [
                    p
                    for p in self.cache.glob(digest + ".*")
                    if p.suffix in (".jpg", ".png", ".gif")
                ]
                try:
                    if cached:
                        path = cached[0]
                        path.touch()
                    else:
                        session = await self.http()
                        async with session.get(url, allow_redirects=False) as response:
                            response.raise_for_status()
                            data = await self.read_limited(response, 8 * 1024 * 1024)
                        data, suffix = await asyncio.to_thread(inspect_image, data)
                        if len(data) > 8 * 1024 * 1024:
                            raise ValueError("Converted image too large")
                        path = self.cache / (digest + suffix)
                        tmp = path.with_name(digest + "." + uuid.uuid4().hex + ".tmp")
                        tmp.write_bytes(data)
                        tmp.replace(path)
                    self.last[(origin, key)] = url
                    self.last.move_to_end((origin, key))
                    while len(self.last) > 1024:
                        self.last.popitem(last=False)
                    self.trim_cache(path)
                    return path
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    OSError,
                    ValueError,
                    PILImage.DecompressionBombError,
                ) as exc:
                    error = exc
                    self.bad_until[url] = time.monotonic() + 300
                    if len(self.bad_until) > 2048:
                        self.bad_until = {
                            u: t for u, t in self.bad_until.items() if t > time.monotonic()
                        }
            raise RuntimeError("No usable image in this attempt") from error

    def trim_cache(self, keep):
        files = sorted(
            (
                p
                for p in self.cache.iterdir()
                if p.is_file() and p.suffix in (".jpg", ".png", ".gif")
            ),
            key=lambda p: p.stat().st_mtime,
        )
        budget = max(16, min(512, int(self.config.get("max_cache_mb", 128)))) * 1024 * 1024
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= budget:
                break
            if path == keep or time.time() - path.stat().st_mtime < 120:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size

    def take_cooldown(self, owner):
        now = time.monotonic()
        cooldown = max(1, min(60, int(self.config.get("cooldown_seconds", 5))))
        if now - self.cooldowns.get(owner, -cooldown) < cooldown:
            return False
        self.cooldowns[owner] = now
        self.cooldowns.move_to_end(owner)
        while len(self.cooldowns) > 2048:
            self.cooldowns.popitem(last=False)
        return True

    async def auto_from_text(self, event, text):
        if not event.get_group_id() or not self.config.get("auto_trigger_enabled", True):
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        if text.lstrip().startswith(("/", "／")):
            return
        key = detect_ip(
            text,
            {pack[0] for pack in self.enabled_packs()},
            getattr(self, "category_patterns", None),
        )
        if key is None:
            return
        origin = event.unified_msg_origin
        owner = (origin, event.get_sender_id())
        # Don't queue a burst of image downloads while a group is awaiting one.
        if origin in self.auto_busy or not self.take_cooldown(owner):
            return
        self.auto_busy.add(origin)
        task = asyncio.create_task(self.prepare(key, origin))
        self.tasks.add(task)
        try:
            async with asyncio.timeout(35):
                path = await task
            # AstrBot 4.28 skips its AI stage when event.send() marks an event
            # as already answered. A spontaneous meme is only an extra image:
            # restore our send flag so the original @ wake-up can still reply.
            previous_send = getattr(event, "_has_send_oper", None)
            try:
                await event.send(
                    event.chain_result([Image.fromFileSystem(str(path))]).use_t2i(False)
                )
            finally:
                if previous_send is not None:
                    event._has_send_oper = previous_send
            logger.info("[柚铃随机表情包] 自动触发 %s，发送完成", key)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            logger.warning("[柚铃随机表情包] 自动触发 %s 暂不可用: %s", key, type(exc).__name__)
        except Exception:
            logger.exception("[柚铃随机表情包] 自动发图发送未确认，不自动重试")
        finally:
            self.tasks.discard(task)
            self.auto_busy.discard(origin)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=110)
    async def on_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        text = "".join(part.text for part in event.get_messages() if isinstance(part, Plain))
        command = parse_command(text, getattr(self, "category_aliases", None))
        if command is None:
            await self.auto_from_text(event, text)
            collector = getattr(self, "collector", None)
            if collector and not text.lstrip().startswith(("/", "／")):
                task = asyncio.create_task(
                    collector.maybe_collect(
                        event, [part for part in event.get_messages() if isinstance(part, Image)]
                    )
                )
                self.tasks.add(task)
                try:
                    await task
                finally:
                    self.tasks.discard(task)
            return
        event.should_call_llm(False)
        action, value = command
        if action == "menu":
            event.set_result(event.plain_result(self.menu(value)).use_t2i(False))
            event.stop_event()
            return
        if action == "ips":
            event.set_result(event.plain_result(self.ip_list()).use_t2i(False))
            event.stop_event()
            return
        if action == "usage":
            event.set_result(
                event.plain_result(
                    "用法：/表情包IP列表、/随机表情包 [页码或分类]、/随机初音、/随机mygo、/随机分类表情包；猪猪：/pig、/pig status、/pig update"
                ).use_t2i(False)
            )
            event.stop_event()
            return
        packs = self.enabled_packs()
        if action == "random":
            packs = [p for p in packs if p[0] in INDEX_SOURCES or self.local_images(p[0])]
        if not packs or (
            action in {"image", "pig_status", "pig_update"} and value not in {p[0] for p in packs}
        ):
            event.set_result(event.plain_result("这个分类目前未启用。发送 /随机表情包 查看菜单。"))
            event.stop_event()
            return
        key = random.choice(packs)[0] if action == "random" else value
        if action in {"pig_status", "pig_update"}:
            count = len(self.local_images(key))
            text = f"猪猪本地图库：{count} 张；由柚铃随机表情包统一管理，发图仅使用本地文件。"
            if action == "pig_update":
                text = f"猪猪本地图库已重新扫描，共 {count} 张；本操作不联网。新图片可在后台猪猪分类上传。"
            event.set_result(event.plain_result(text).use_t2i(False))
            event.stop_event()
            return
        if key not in INDEX_SOURCES and not self.local_images(key):
            event.set_result(
                event.plain_result("这个分类还没有本地图片，请先在后台上传。").use_t2i(False)
            )
            event.stop_event()
            return
        owner = (event.unified_msg_origin, event.get_sender_id())
        if not self.take_cooldown(owner):
            event.set_result(event.plain_result("稍等几秒再抽一张吧。"))
            event.stop_event()
            return
        task = asyncio.create_task(self.prepare(key, event.unified_msg_origin))
        self.tasks.add(task)
        try:
            async with asyncio.timeout(35):
                path = await task
            event.set_result(
                event.chain_result(
                    [Plain(text=f"【{self.category_title(key)}】"), Image.fromFileSystem(str(path))]
                ).use_t2i(False)
            )
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            logger.warning("[柚铃随机表情包] %s unavailable: %s", key, type(exc).__name__)
            event.set_result(event.plain_result("这次图片源暂时不可用，请稍后重试或换个分类。"))
        finally:
            self.tasks.discard(task)
        event.stop_event()

    async def terminate(self):
        web_gallery = getattr(self, "web_gallery", None)
        if web_gallery:
            web_gallery.close()
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.session and not self.session.closed:
            await self.session.close()
