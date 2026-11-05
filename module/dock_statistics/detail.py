import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from module.base.button import Button
from module.base.template import Template
from module.base.utils import color_similarity_2d, crop
from module.dock_statistics.assets import *
from module.ocr.ocr import Digit, Ocr

# Areas are measured on CN server, ship detail page, "detail" tab of the side navbar.


def _area(name, area):
    return Button(area=area, color=(), button=area, name=name)


STAR_GOLD = (255, 219, 74)

# Narrower than the name bar, to skip the rename icon on the left and the mood icon on the right
OCR_NAME = Ocr(_area('DETAIL_NAME', (247, 90, 418, 116)), lang='cnocr', threshold=160, name='OCR_DETAIL_NAME')
# Small english name under the chinese name, best effort, helps to identify ships when chinese name goes wrong
OCR_NAME_EN = Ocr(_area('DETAIL_NAME_EN', (236, 117, 396, 133)), lang='cnocr', name='OCR_DETAIL_NAME_EN')
OCR_LEVEL = Digit(_area('DETAIL_LEVEL', (752, 286, 805, 318)), name='OCR_DETAIL_LEVEL')
OCR_EXP = Ocr(_area('DETAIL_EXP', (1120, 284, 1248, 306)), alphabet='0123456789/MAX', name='OCR_DETAIL_EXP')
OCR_SKILL = Ocr([_area(f'DETAIL_SKILL_{i}', (x + 85, 570, x + 178, 600)) for i, x in enumerate([690, 876, 1064])],
                letter=(115, 115, 115), threshold=64, alphabet='LEV:0123456789', name='OCR_DETAIL_SKILL')
STARS_AREA = (300, 62, 425, 88)
# Left edge of the gold center line of each star in STARS_AREA. Stars are right aligned, filled from the left,
# 5-star ships have no star in the first slot.
STAR_SLOTS = [15, 33, 50, 68, 85, 103]
# On the center line of a filled star, rows of pale gold body and the bright gold core below it
STAR_BODY_ROWS = slice(8, 16)
STAR_CORE_ROWS = slice(17, 19)
AFFINITY_AREA = (565, 95, 665, 175)
AFFINITY_TEMPLATES = {
    'oath': TEMPLATE_AFFINITY_OATH,
    'love': TEMPLATE_AFFINITY_LOVE,
    'like': TEMPLATE_AFFINITY_LIKE,
    'stranger': TEMPLATE_AFFINITY_STRANGER,
    # META ships
    'resonance': TEMPLATE_AFFINITY_RESONANCE,
    # THE iDOLM@STER collab ships show "应援" instead, affinity page still works
    'idol': TEMPLATE_AFFINITY_IDOL,
}
# Lock button at the bottom, it says "解锁" if the ship is locked, "锁定" if not
LOCK_AREA = (860, 650, 960, 705)
# Areas that slide in after switching ships: name and stars, level and exp, skill levels, lock button.
# Small areas, so a few changing digits are not averaged away.
# Skill names are excluded, long names keep scrolling.
DETAIL_STABLE_AREAS = [(140, 60, 480, 140), (690, 286, 1250, 318), (690, 565, 1250, 605), LOCK_AREA]

# Affinity page, the marriage certificate opened by tapping the affinity icon.
# Certificate layouts differ between factions, but all have two requirement rows at the bottom right,
# each starts with a "已达成" or "未达成" box, the first row is "好感度达到100(当前200)".
AFFINITY_ICON = _area('AFFINITY_ICON', (595, 115, 635, 150))
AFFINITY_ROW_SEARCH_AREA = (700, 450, 1280, 650)
# Value text relative to the top left of the requirement box
AFFINITY_VALUE_OFFSET = (74, -5, 300, 24)
# Rows are 52-61px apart. The first box may be missed when painting behind its translucent background is bright,
# then the found box is on the second row, try rows above it.
AFFINITY_ROW_SHIFTS = [0, -55, -52, -58, -61]
# Dimmed areas outside the certificate, tap to close it. Bottom center doesn't work on Dragon Empery ones.
AFFINITY_PAGE_CLOSE = [
    _area('AFFINITY_PAGE_CLOSE', (1235, 690, 1260, 705)),
    _area('AFFINITY_PAGE_CLOSE_2', (620, 685, 660, 700)),
]


@dataclass
class AffinityLayout:
    """
    Certificate layouts that ship name and oath date can be read from.
    """
    name: str
    check: Template
    check_area: Tuple[int, int, int, int]
    # Ship name, dark text on paper, more reliable than the one on detail page
    name_area: Tuple[int, int, int, int]
    # Oath date like "February.09,26", oathed ships only
    date_area: Tuple[int, int, int, int]


AFFINITY_LAYOUTS = [
    AffinityLayout(name='western', check=TEMPLATE_AFFINITY_PAGE, check_area=(180, 515, 360, 570),
                   name_area=(392, 568, 568, 592), date_area=(260, 490, 420, 514)),
    # META ships, dark paper with red labels
    AffinityLayout(name='meta', check=TEMPLATE_AFFINITY_PAGE_META, check_area=(190, 525, 370, 570),
                   name_area=(440, 575, 580, 600), date_area=(270, 493, 440, 517)),
]


@dataclass
class ShipDetail:
    category: str = ''
    index: int = 0
    name: str = ''
    name_en: str = ''
    level: int = 0
    exp: str = ''
    stars: int = 0
    affinity: str = ''
    affinity_value: Optional[int] = None
    oath_date: str = ''
    skills: List[int] = field(default_factory=list)

    def problems(self) -> List[str]:
        """
        Returns:
            list[str]: Fields that look unrecognized
        """
        problems = []
        if not self.name:
            problems.append('name')
        if not 1 <= self.level <= 125:
            problems.append('level')
        if not 1 <= self.stars <= 6:
            problems.append('stars')
        if self.affinity == 'unknown':
            problems.append('affinity')
        # Affinity page of META ships is not handled
        if self.affinity_value is None and self.affinity != 'resonance':
            problems.append('affinity_value')
        return problems

    def apply_affinity_page(self, value, name, oath_date):
        """
        Merge results of scan_affinity_page().
        The name on certificate is more reliable, it replaces the one from detail page if recognized.
        """
        self.affinity_value = value
        if name:
            self.name = name
        if self.affinity == 'oath':
            self.oath_date = oath_date


def normalize_name(name: str) -> str:
    """
    Fix OCR confusions in ship names.
    Roman numeral after Chinese characters is always "II" on CN, its two thin bars are often read as one.
        约克城lI -> 约克城II
        列克星敦I -> 列克星敦II
    Middle dots around a single initial get lost.
        威廉·D波特 -> 威廉·D·波特
        女灶神META -> 女灶神·META
    """
    # Leading dot shows when a long name gets cut, ·加利索尼埃·META
    name = name.strip().strip('_-.,，一—·()（）、`\'"')
    name = re.sub(r'([一-鿿])[lI1|‖]+$', r'\1II', name)
    name = re.sub(r'·([A-Z])([一-鿿])', r'·\1·\2', name)
    # Long META names get cut, 加利索尼埃ME -> 加利索尼埃·META
    name = re.sub(r'([一-鿿])·?M(E(TA?)?)?$', r'\1·META', name)
    return name


def parse_exp(text: str) -> str:
    """
    Args:
        text: OCR result like "1487893/M8X" or "1500/2600"

    Returns:
        str: "1487893/MAX", "1500/2600", or "" if unrecognized
    """
    res = re.match(r'^(\d+)/(.+)$', text)
    if not res:
        return ''
    current, total = res.groups()
    if total.startswith('M'):
        return f'{current}/MAX'
    total = re.sub(r'\D', '', total)
    return f'{current}/{total}' if total else ''


def parse_skill(text: str) -> int:
    """
    Args:
        text: OCR result like "LEVEL:10" or "LEVEL10"
    """
    res = re.search(r'(\d+)$', text)
    return int(res.group(1)) if res else 0


def _fix_digits(text: str) -> str:
    return text.translate(str.maketrans('lI|Oo', '11100'))


def parse_affinity_value(text: str) -> Optional[int]:
    """
    Args:
        text: OCR result like "好感度达到100(当前200)", "当" is sometimes read as "铛", "1" as "l".
            The "100" before current value is required, to reject partially recognized text.

    Returns:
        int: Affinity value in range [0, 200], or None
    """
    res = re.search(r'1[0Oo]{2}\W{0,2}[当铛]?前\s*([0-9lI|Oo]+)', text)
    if not res:
        return None
    value = int(_fix_digits(res.group(1)))
    return value if 0 <= value <= 200 else None


MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october',
          'november', 'december']


def parse_oath_date(text: str) -> str:
    """
    Args:
        text: OCR result like "February.09,26", "October.l0,26", "June.07，21"

    Returns:
        str: Date like "2026-02-09", or the original text if unrecognized
    """
    res = re.search(r'([A-Za-z]{3,})\W*([0-9lI|Oo]{1,2})\W+([0-9lI|Oo]{2})', text)
    if not res:
        return text
    month, day, year = res.groups()
    month = [i for i, m in enumerate(MONTHS, start=1) if m.startswith(month[:3].lower())]
    if not month:
        return text
    return f'20{_fix_digits(year)}-{month[0]:02d}-{int(_fix_digits(day)):02d}'


def count_stars(image) -> int:
    """
    Count filled (gold) limit break stars.
    """
    similarity = color_similarity_2d(crop(image, STARS_AREA), color=STAR_GOLD)
    count = 0
    # Stars are translucent, gold painting behind them shows through, so check the profile of the center line
    # instead of counting gold columns. Empty stars are dark on the center line.
    for slot in STAR_SLOTS:
        line = similarity[:, slot:slot + 2].min(axis=1)
        if np.all(line[STAR_BODY_ROWS] > 150) and np.all(line[STAR_CORE_ROWS] > 200):
            count += 1
    return count


def parse_level(level: int) -> int:
    """
    The "认知觉醒" label after level of awakened ships is read as an extra digit.
        1201 -> 120
    """
    while level > 125:
        level //= 10
    return level


def classify_affinity(image, similarity=0.6) -> str:
    """
    Returns:
        str: 'oath', 'love', 'like', 'stranger', 'resonance', 'idol', or 'unknown' if no template matches
    """
    area = crop(image, AFFINITY_AREA)
    scores = sorted([(template.match_result(area)[0], name) for name, template in AFFINITY_TEMPLATES.items()],
                    reverse=True)
    (best, name), (second, _) = scores[0], scores[1]
    # Background behind the icon varies, wrong templates stay below 0.4
    return name if best >= similarity and best - second >= 0.2 else 'unknown'


def detail_locked(image, similarity=0.65) -> Optional[bool]:
    """
    Returns:
        bool: True if locked, False if not, None if lock button not found
    """
    area = crop(image, LOCK_AREA)
    locked = TEMPLATE_SHIP_LOCKED.match_result(area)[0]
    unlocked = TEMPLATE_SHIP_UNLOCKED.match_result(area)[0]
    # Each template scores about 0.45 on the other button
    if max(locked, unlocked) < similarity or abs(locked - unlocked) < 0.2:
        return None
    return locked > unlocked


# Text colors of the value row, white if the requirement is reached, dim brown if not, grey on some layouts
AFFINITY_TEXT_REACHED = ((255, 255, 255), 160)
AFFINITY_TEXT_NOT_REACHED = ((150, 140, 120), 64)
AFFINITY_TEXT_GREY = ((160, 160, 160), 64)


def affinity_row_anchor(image) -> Optional[Tuple[int, int, bool]]:
    """
    Returns:
        tuple[int, int, bool]: Top left of the first requirement box on affinity page, and if it's reached.
            None if not on affinity page.
    """
    x0, y0, _, _ = AFFINITY_ROW_SEARCH_AREA
    area = crop(image, AFFINITY_ROW_SEARCH_AREA)
    # Boxes are translucent, they score 0.78+ over dark paintings. The two templates stay below 0.7 on each other.
    boxes = [(box.area[0], box.area[1], True) for box in TEMPLATE_AFFINITY_REACHED.match_multi(area, similarity=0.7)]
    boxes += [(box.area[0], box.area[1], False)
              for box in TEMPLATE_AFFINITY_NOT_REACHED.match_multi(area, similarity=0.7)]
    if not boxes:
        return None
    x, y, reached = min(boxes, key=lambda b: b[1])
    return x0 + x, y0 + y, reached


def affinity_page_appear(image) -> bool:
    return affinity_row_anchor(image) is not None


def affinity_page_layout(image) -> Optional[AffinityLayout]:
    """
    Returns:
        AffinityLayout: Layout that ship name and oath date can be read from, or None
    """
    for layout in AFFINITY_LAYOUTS:
        if layout.check.match(crop(image, layout.check_area)):
            return layout
    return None


def scan_affinity_page(image) -> Tuple[Optional[int], str, str]:
    """
    Pages:
        in: affinity page

    Returns:
        int: Affinity value, or None if unrecognized
        str: Ship name, or "" if layout unknown
        str: Oath date, or "" if layout unknown, meaningful on oathed ships only
    """
    value = None
    anchor = affinity_row_anchor(image)
    if anchor is not None:
        x, y, reached = anchor
        for shift in AFFINITY_ROW_SHIFTS:
            area = tuple(int(v) for v in np.add((x, y + shift, x, y + shift), AFFINITY_VALUE_OFFSET))
            # Box on the anchor row tells text color, rows above it are unknown
            if shift == 0 and not reached:
                colors = [AFFINITY_TEXT_NOT_REACHED, AFFINITY_TEXT_GREY, AFFINITY_TEXT_REACHED]
            else:
                colors = [AFFINITY_TEXT_REACHED, AFFINITY_TEXT_NOT_REACHED, AFFINITY_TEXT_GREY]
            for letter, threshold in colors:
                ocr = Ocr(_area('AFFINITY_VALUE', area), lang='cnocr', letter=letter, threshold=threshold,
                          name='OCR_AFFINITY_VALUE')
                value = parse_affinity_value(ocr.ocr(image, direct_ocr=False))
                if value is not None:
                    break
            if value is not None:
                break

    name, oath_date = '', ''
    layout = affinity_page_layout(image)
    if layout is not None:
        ocr = Ocr(_area('AFFINITY_NAME', layout.name_area), lang='cnocr', letter=(80, 80, 80), threshold=128,
                  name='OCR_AFFINITY_NAME')
        name = normalize_name(ocr.ocr(image, direct_ocr=False))
        ocr = Ocr(_area('AFFINITY_OATH_DATE', layout.date_area), lang='cnocr', letter=(90, 90, 90), threshold=128,
                  name='OCR_AFFINITY_OATH_DATE')
        oath_date = parse_oath_date(ocr.ocr(image, direct_ocr=False).strip(' _-'))
    return value, name, oath_date


def scan_detail(image, category='', index=0) -> ShipDetail:
    """
    Args:
        image: Screenshot of ship detail page, detail tab.
        category: Dock filter index the ship was found under.
        index: 1-based position in that category.

    Pages:
        in: SHIP_DETAIL_CHECK, detail tab
    """
    skills = OCR_SKILL.ocr(image, direct_ocr=False)
    return ShipDetail(
        category=category,
        index=index,
        name=normalize_name(OCR_NAME.ocr(image, direct_ocr=False)),
        name_en=OCR_NAME_EN.ocr(image, direct_ocr=False).strip(' _-'),
        level=parse_level(OCR_LEVEL.ocr(image, direct_ocr=False)),
        exp=parse_exp(OCR_EXP.ocr(image, direct_ocr=False)),
        stars=count_stars(image),
        affinity=classify_affinity(image),
        skills=[parse_skill(s) for s in skills],
    )
