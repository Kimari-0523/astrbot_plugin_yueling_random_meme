"""Public emoji indexes from koishi-shangxue-plugins/koishi-shangxue-apps."""

import re
import unicodedata

SOURCE = "https://github.com/koishi-shangxue-plugins/koishi-shangxue-apps/tree/main/plugins/emojihub-bili"
PACKS = [
    (
        "miku",
        "初音未来 / Fufu",
        "初音未来Q.txt",
        ["初音", "初音未来", "miku", "hatsunemiku", "fufu"],
    ),
    (
        "shigure_ui",
        "时雨羽衣",
        None,
        [
            "时雨羽衣",
            "時雨羽衣",
            "羽衣",
            "羽衣妈妈",
            "羽衣妈",
            "ういママ",
            "しぐれうい",
            "shigureui",
            "shigure ui",
            "有妈",
            "妈妈",
            "妈咪",
            "麻麻",
        ],
    ),
    ("pig", "猪猪", None, ["猪猪", "猪", "祝", "🐷", "🐖", "🐽", "㊗", "㊗️"]),
    ("bocchi", "孤独摇滚", "孤独摇滚.txt", ["孤独摇滚", "bocchi"]),
    ("kemomimi", "兽耳酱", "kemomimi酱表情包.txt", ["兽耳酱", "kemomimi"]),
    ("mygo", "MyGO", "mygo.txt", ["mygo"]),
    ("kagura_nana", "狗妈", "狗妈.txt", ["狗妈", "kagura_nana"]),
    ("ba", "蔚蓝档案", "ba.txt", ["蔚蓝档案", "碧蓝档案", "ba"]),
    ("cheshire", "柴郡猫", "柴郡.txt", ["柴郡", "柴郡猫", "cheshire"]),
    ("yuzusoft", "柚子社", "0721.txt", ["柚子社", "0721", "yuzusoft"]),
    ("doro", "Doro", "doro.txt", ["doro"]),
    ("eveonecat", "Eveonecat", "eveonecat.txt", ["eveonecat"]),
    ("natsuki", "甘城猫猫", "甘城猫猫.txt", ["甘城猫猫", "natsuki"]),
    ("gbc", "少女乐队的呐喊", "GirlsBandCry.txt", ["gbc", "少哭乐队", "girlsbandcry"]),
    ("capoo", "咖波", "capoo.txt", ["咖波", "capoo"]),
    ("chiikawa", "吉伊卡哇", "chiikawa.txt", ["吉伊卡哇", "吉伊", "chiikawa"]),
]
ALIASES = {alias.casefold(): pack[0] for pack in PACKS for alias in [pack[0], *pack[3]]}
BY_KEY = {pack[0]: pack for pack in PACKS}
INDEX_SOURCES = {pack[0]: (pack[2],) for pack in PACKS if pack[2]}
INDEX_SOURCES["miku"] = ("初音未来Q.txt", "fufu.txt")
LOCAL_KEYS = {"shigure_ui", "pig"}


def trigger_names(pack):
    """Display the exact names used by ordinary group-message matching."""
    names = {}
    for name in [pack[1], *([] if pack[0].startswith("custom_") else [pack[0]]), *pack[3]]:
        normalized = unicodedata.normalize("NFKC", name).casefold()
        # 0721 is an index nickname, not an IP name; don't match dates/orders.
        if normalized and not normalized.isdecimal():
            names.setdefault(normalized, name)
    return list(names.values())


def build_patterns(packs):
    patterns = []
    for pack in packs:
        for name in trigger_names(pack):
            name = unicodedata.normalize("NFKC", name).casefold()
            pattern = re.escape(name)
            if name.isascii():
                pattern = r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])"
            patterns.append((pack[0], name, re.compile(pattern)))
    return patterns


AUTO_PATTERNS = build_patterns(PACKS)


def detect_ip(text, enabled_keys=None, patterns=None):
    """Pick the first mentioned enabled IP, longest name winning a position tie."""
    text = unicodedata.normalize("NFKC", text).casefold()
    matches = []
    for key, name, pattern in AUTO_PATTERNS if patterns is None else patterns:
        if enabled_keys is not None and key not in enabled_keys:
            continue
        match = pattern.search(text)
        if match:
            matches.append((match.start(), -len(name), key))
    return min(matches)[2] if matches else None


def parse_command(text, aliases=None):
    aliases = ALIASES if aliases is None else aliases
    text = text.strip().lstrip("/／").strip()
    if text.startswith("随机") and text[2:].casefold() in aliases:
        return "image", aliases[text[2:].casefold()]
    pieces = text.split()
    if not pieces:
        return None
    word, args = pieces[0].casefold(), pieces[1:]
    if word == "pig":
        if "pig" not in aliases:
            return None
        if not args:
            return "image", aliases["pig"]
        if len(args) == 1:
            if args[0].casefold() in {"status", "状态"}:
                return "pig_status", aliases["pig"]
            if args[0].casefold() in {"update", "更新"}:
                return "pig_update", aliases["pig"]
        return "usage", None
    if word in ("表情包ip列表", "随机表情包ip列表", "表情包作品列表"):
        return ("ips", None) if not args else ("usage", None)
    if word in ("随机表情包", "表情包列表"):
        if not args:
            return "menu", 1
        if len(args) == 1 and args[0].casefold() in ("ip", "ip列表", "所有ip", "作品", "作品列表"):
            return "ips", None
        if len(args) == 1 and args[0].isdigit():
            return "menu", int(args[0])
        if " ".join(args).casefold() in aliases:
            return "image", aliases[" ".join(args).casefold()]
        return "usage", None
    if word == "随机分类表情包":
        return ("random", None) if not args else ("usage", None)
    if word.startswith("随机") and word[2:] in aliases:
        return ("image", aliases[word[2:]]) if not args else ("usage", None)
    return None
