"""Persistent custom categories, separate from bundled catalog and images."""

import json
import re
import threading
import unicodedata
import uuid
from pathlib import Path

from .catalog import PACKS


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def checked_word(value, limit):
    if not isinstance(value, str):
        raise ValueError("分类名称和触发词须为文字")
    value = unicodedata.normalize("NFKC", value).strip()
    if (
        not value
        or len(value) > limit
        or any(c in value for c in "/\\")
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise ValueError(f"名称和触发词不能为空、不能含斜杠或控制字符，且最多 {limit} 字")
    if value.isdecimal():
        raise ValueError("请使用名称或昵称，不要使用纯数字触发词")
    return value


class CategoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.custom = []
        if self.path.exists():
            document = json.loads(self.path.read_text(encoding="utf-8"))
            rows = document.get("categories")
            if document.get("version") != 1 or not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("自定义分类数据格式错误")
            for row in rows:
                if not isinstance(row, dict) or not re.fullmatch(
                    r"custom_[0-9a-f]{12}", str(row.get("key", ""))
                ):
                    raise ValueError("自定义分类编号错误")
                title, aliases = self.validate(row.get("title"), row.get("aliases"))
                if any(p[0] == row["key"] for p in self.packs()):
                    raise ValueError("自定义分类编号重复")
                self.custom.append((row["key"], title, None, aliases))

    def packs(self):
        return [*PACKS, *self.custom]

    def validate(self, title, aliases):
        title = checked_word(title, 32)
        if isinstance(aliases, str):
            aliases = [x.strip() for x in re.split(r"[,，;；\n\r]+", aliases) if x.strip()]
        if not isinstance(aliases, list) or len(aliases) > 64:
            raise ValueError("最多填写 64 个触发词（含分类名称），用逗号或换行分隔")
        names = {normalized(title): title}
        for alias in aliases:
            word = checked_word(alias, 40)
            names.setdefault(normalized(word), word)
        if len(names) > 64:
            raise ValueError("最多填写 64 个触发词（含分类名称），用逗号或换行分隔")
        taken = {
            normalized(name): pack[1]
            for pack in self.packs()
            for name in [pack[0], pack[1], *pack[3]]
        }
        for key, word in names.items():
            if key in {
                "分类表情包",
                "表情包",
                "表情包列表",
                "表情包ip列表",
                "随机表情包",
                "随机分类表情包",
                "随机表情包ip列表",
                "表情包作品列表",
            }:
                raise ValueError(f"“{word}”与现有菜单指令冲突，请换一个名称或触发词")
            if key in taken:
                raise ValueError(f"“{word}”已属于分类“{taken[key]}”，请换一个名称或触发词")
        return title, list(names.values())

    def create(self, title, aliases):
        with self.lock:
            if len(self.custom) >= 100:
                raise ValueError("最多添加 100 个自定义分类")
            title, aliases = self.validate(title, aliases)
            key = "custom_" + uuid.uuid4().hex[:12]
            row = (key, title, None, aliases)
            all_custom = [*self.custom, row]
            document = {
                "version": 1,
                "categories": [{"key": p[0], "title": p[1], "aliases": p[3]} for p in all_custom],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.pending")
            temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
            self.custom = all_custom
            return row
