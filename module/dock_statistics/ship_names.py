"""
Official CN ship names from BWIKI, to fix ship names from OCR.

Update the list at ALAS root folder:
    python -m module.dock_statistics --update-names
"""
import difflib
import html
import json
import os
import re
from functools import lru_cache
from typing import Dict, List

SHIP_NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ship_names.json')
# 舰船图鉴, cards of all ships, each has the ship name and its censored name if any
WIKI_URL = 'https://wiki.biligame.com/blhx/%E8%88%B0%E8%88%B9%E5%9B%BE%E9%89%B4'
WIKI_CARD = re.compile(
    r'<div class="jntj-1 divsort" data-param0="[^"]*" data-param1="([^"]*)" data-param2="[^"]*" '
    r'data-param3="([^"]*)" data-param4="[^"]*">.*?<span class="jntj-4">(.*?)</span>', re.S)
# Hull types on wiki of each dock filter index, ships of the same category are preferred when matching
CATEGORY_TYPES = {
    'dd': ['驱逐'],
    'cl': ['轻巡'],
    'ca': ['重巡', '超巡'],
    'bb': ['战列', '战巡'],
    'cv': ['航母', '轻航'],
    'repair': ['维修'],
    'ss': ['潜艇', '潜母', '风帆S'],
    'others': ['运输', '重炮', '风帆V', '风帆M', '风帆S', '航战', '战列'],
}
# Retrofitted ships are named like "贝尔法斯特·改" or "圣地亚哥.改" in game, wiki has their base names only
RETROFIT = re.compile(r'[.·]改$')
# Wiki tells ships of the same name apart by suffix, they don't have it in game, 天城CV, 莲SSSS, 霞DOA
WIKI_SUFFIX = re.compile(r'(?<=[一-鿿])(CV|BB|JP|DE|DOA|SSSS)$')
# Ships not on the wiki page
EXTRA_SHIPS = [
    {'name': name, 'alias': [], 'type': hull, 'faction': 'hololive'}
    for name, hull in [('白上吹雪', '驱逐'), ('夏色祭', '驱逐'), ('百鬼绫目', '重巡'), ('时乃空', '轻航'),
                       ('大神澪', '航母'), ('紫咲诗音', '轻航'), ('湊阿库娅', '潜艇')]
]


def update_ship_names(file=SHIP_NAMES_FILE) -> List[Dict]:
    """
    Download ship names from wiki and save to ship_names.json
    """
    import requests
    resp = requests.get(WIKI_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
    resp.raise_for_status()
    ships = []
    for hull, faction, span in WIKI_CARD.findall(resp.text):
        # <a href=... title="长门">长门<br />鲨</a>
        span = re.sub(r'</?a[^>]*>', '', span)
        names = [html.unescape(re.sub(r'<[^>]+>', '', s)).strip() for s in re.split(r'<br\s*/?>', span)]
        ships.append({'name': WIKI_SUFFIX.sub('', names[0]), 'alias': [n for n in names[1:] if n],
                      'type': hull.split(',')[-1], 'faction': faction})
    # Page layout changed, don't overwrite the existing list
    if len(ships) < 500:
        raise ValueError(f'Only {len(ships)} ships found on wiki, page layout may have changed')
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(ships, f, ensure_ascii=False, indent=1)
    load_ship_names.cache_clear()
    return ships


@lru_cache(maxsize=None)
def load_ship_names(file=SHIP_NAMES_FILE) -> List[Dict]:
    if not os.path.exists(file):
        return []
    with open(file, encoding='utf-8') as f:
        return json.load(f) + EXTRA_SHIPS


def _split_retrofit(name):
    res = RETROFIT.search(name)
    return (name[:res.start()], res.group()) if res else (name, '')


def _plain(name):
    # Middle dots are often lost in OCR
    return name.replace('·', '')


def _collapse(name):
    # Characters are read twice sometimes, 狮狮, 斯斯彭斯
    return re.sub(r'(.)\1+', r'\1', name)


def _closest(name, pool, same_category):
    """
    Args:
        name: Ship name from OCR, without retrofit suffix
        pool: Official names to choose from
        same_category: If pool has ships of the same category only, looser rules are used
    """
    plain = _plain(name)
    # Long names scroll in the name bar, screenshot shows only a part of it.
    # Rare characters may be lost, 杓 for 杓鹬
    if len(plain) >= (1 if same_category else 3):
        hits = [n for n in pool if plain in _plain(n)]
        if len(hits) == 1:
            return hits[0]
    scores = sorted(((difflib.SequenceMatcher(None, plain, _plain(n)).ratio(), n) for n in pool), reverse=True)
    if scores and scores[0][0] >= 0.75 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.1):
        return scores[0][1]
    if same_category and len(plain) >= 2:
        # One character read wrong in short names, 阿武限 for 阿武隈, 比权 for 比叡
        hits = [n for n in pool if len(_plain(n)) == len(plain)
                and sum(a != b for a, b in zip(plain, _plain(n))) == 1]
        if len(hits) == 1:
            return hits[0]
    return None


def is_known_ship_name(name: str) -> bool:
    """
    Returns:
        bool: If name is in the list, True if the list is not downloaded
    """
    ships = load_ship_names()
    if not ships:
        return True
    base, _ = _split_retrofit(name)
    return any(s['name'] == base for s in ships)


def match_ship_name(name: str, category: str = '') -> str:
    """
    Args:
        name: Ship name from OCR
        category: Dock filter index the ship was found under, such as 'dd'

    Returns:
        str: Official name closest to it, retrofit suffix kept. Or the given name if nothing is close.
            娜·阿丝达·戴比跆 -> 娜娜·阿丝达·戴比路克
            文琴佐焦贝蒂 -> 文琴佐·焦贝蒂
            鲨 -> 长门, censored name if uncensored is not applied
    """
    ships = load_ship_names()
    if not ships or not name:
        return name
    base, suffix = _split_retrofit(name)
    names = {s['name'] for s in ships}
    if base in names:
        return name
    if _collapse(base) in names:
        return _collapse(base) + suffix
    types = CATEGORY_TYPES.get(category, [])
    for ship in ships:
        # Censored names are short and reused, 莲 is 雪风 censored and also a ship of SSSS
        if base in ship['alias'] and (not types or ship['type'] in types):
            return ship['name'] + suffix
    # Some retrofits are listed on wiki as well, 杓鹬.改
    preferred = sorted({_split_retrofit(s['name'])[0] for s in ships if s['type'] in types})
    everything = sorted({_split_retrofit(s['name'])[0] for s in ships})
    for pool, same_category in [(preferred, True), (everything, False)]:
        match = _closest(base, pool, same_category=same_category)
        if match:
            return match + suffix
    return name
