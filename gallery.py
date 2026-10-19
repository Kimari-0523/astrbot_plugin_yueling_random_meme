"""Validated, persistent image imports. No network or caller-controlled paths."""

import hashlib
import io
import threading
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageSequence

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LIBRARY_BYTES = 512 * 1024 * 1024
MAX_FRAME_PIXELS = 20_000_000
MAX_TOTAL_PIXELS = 80_000_000
MAX_FRAMES = 240
SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}


def normalize_image(data):
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("每张图片须为 1 字节至 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt = image.format
                if fmt not in {"JPEG", "PNG", "GIF", "WEBP"}:
                    raise ValueError("只支持 JPG、PNG、GIF、WebP 图片")
                width, height = image.size
                count = getattr(image, "n_frames", 1)
                if (
                    width * height > MAX_FRAME_PIXELS
                    or count > MAX_FRAMES
                    or width * height * count > MAX_TOTAL_PIXELS
                ):
                    raise ValueError("图片尺寸或动画帧数过大")
                if fmt == "WEBP" and width * height * count > 20_000_000:
                    raise ValueError("WebP 动画过大，请先压缩或转换为 GIF")
                frames, durations = [], []
                for frame in ImageSequence.Iterator(image):
                    frame.load()
                    if fmt == "WEBP":
                        frames.append(frame.convert("RGBA"))
                        durations.append(frame.info.get("duration", 100))
                if fmt == "WEBP":
                    output = io.BytesIO()
                    if count > 1:
                        frames[0].save(
                            output,
                            format="GIF",
                            save_all=True,
                            append_images=frames[1:],
                            duration=durations,
                            loop=0,
                            disposal=2,
                        )
                        suffix = ".gif"
                    else:
                        frames[0].save(output, format="PNG")
                        suffix = ".png"
                    data = output.getvalue()
                else:
                    suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}[fmt]
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("转换后的图片超过 8 MiB，请先压缩")
        return data, suffix, {"width": width, "height": height, "frames": count}
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("图片损坏、未完整下载或尺寸过大") from exc


class LocalGallery:
    def __init__(self, root, keys):
        self.root = Path(root)
        self.keys = set(keys)
        self.lock = threading.Lock()

    def folder(self, key):
        if key not in self.keys:
            raise ValueError("未知表情分类")
        path = self.root / key
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("图库路径异常")
        return path

    def images(self, key):
        return sorted(
            p
            for p in self.folder(key).glob("*")
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUFFIXES
        )

    def stats(self):
        result = {}
        for key in self.keys:
            images = self.images(key)
            result[key] = {"count": len(images), "bytes": sum(p.stat().st_size for p in images)}
        return result

    def import_image(self, key, raw):
        folder = self.folder(key)
        data, suffix, info = normalize_image(raw)
        name = hashlib.sha256(data).hexdigest() + suffix
        target = folder / name
        with self.lock:
            if target.is_symlink():
                raise ValueError("图库文件路径异常")
            if target.exists():
                return {"added": False, "duplicate": True, "filename": name, **info}
            total = sum(row["bytes"] for row in self.stats().values())
            if total + len(data) > MAX_LIBRARY_BYTES:
                raise ValueError("本地图库已达到 512 MiB，请先联系管理员整理")
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / (uuid.uuid4().hex + ".pending")
            try:
                temp.write_bytes(data)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        return {"added": True, "duplicate": False, "filename": name, **info}
