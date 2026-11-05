"""
Cognitive awakening (认知觉醒) costs, from BWIKI 认知觉醒.

Level cap starts at 100, each awakening raises it by 5 up to 125.
Every stage costs 心智单元, 120→125 costs 心智单元II too. 物资 is 10 times the 心智单元.
Retrofitted ships pay by their rarity before retrofit.
"""
from typing import Dict

STAGES = [100, 105, 110, 115, 120]
# Frame colors of each rarity, plan ships wear the color of their tier
COST_TIER = {'普通': '白', '稀有': '蓝', '精锐': '紫', '超稀有': '金', '最高方案': '金', '海上传奇': '彩', '决战方案': '彩'}
# 心智单元 of each stage from 100→105 to 120→125, and 心智单元II of 120→125
COST = {
    '白': ([60, 120, 180, 300, 80], 40),
    '蓝': ([80, 160, 240, 400, 120], 60),
    '紫': ([120, 240, 360, 600, 200], 100),
    '金': ([200, 400, 600, 1000, 300], 150),
    '彩': ([300, 600, 900, 1500, 450], 225),
}
# Stars of a ship with no limit break and with full limit break.
# Plan ships, 海上传奇 and META ships can awaken without full stars.
BASE_STARS = {'普通': 1, '稀有': 2, '精锐': 2, '超稀有': 3, '海上传奇': 3}
MAX_STARS = {'普通': 4, '稀有': 5, '精锐': 5, '超稀有': 6}


def level_cap(level: int, exp: str) -> int:
    """
    A ship at its cap shows exp as "1487893/MAX", others are working through the next stage.
    """
    if level < 100:
        return 100
    if exp.endswith('/MAX'):
        return level
    return min((level // 5 + 1) * 5, 125)


def is_low_cost(level: int, stars: int, rarity: str) -> bool:
    """
    Low-cost ships (低耗船) are kept at Lv100 with no limit break, to save oil.
    """
    return level == 100 and stars == BASE_STARS.get(rarity)


def cost_tier(rarity: str, stars: int) -> str:
    """
    Args:
        rarity: Rarity on wiki, empty if unknown
        stars: Current stars, guesses the rarity when it's unknown

    Returns:
        str: '白', '蓝', '紫', '金' or '彩'
    """
    if not rarity:
        # A collab ship missing on wiki, an awakened one has full stars: 6 is 超稀有, 5 is 精锐
        rarity = '超稀有' if stars >= 6 else '精锐'
    return COST_TIER[rarity]


def awakening_cost(tier: str, cap: int) -> Dict[str, int]:
    """
    Args:
        tier: Cost tier from cost_tier()
        cap: Current level cap

    Returns:
        dict: chips_to_120, chips_to_125, chips2_to_125
    """
    chips, chips2 = COST[tier]
    need = [i for i, start in enumerate(STAGES) if start >= cap]
    return {
        'chips_to_120': sum(chips[i] for i in need if STAGES[i] < 120),
        'chips_to_125': sum(chips[i] for i in need),
        'chips2_to_125': chips2 if 4 in need else 0,
    }
