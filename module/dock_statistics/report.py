"""
Training report of a dock statistics run, written to <run folder>/report:
    ships_report.csv, every recorded ship with wiki info, level cap and awakening cost
    dock_report.html, one page of charts and tables

Usage, at ALAS root folder:
    python -m module.dock_statistics --report ../AzurLaneDock/20261105_082050
"""
import csv
import html
import json
import os
import re
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Dict, List

from module.dock_statistics.awakening import COST, awakening_cost, cost_tier, is_low_cost, level_cap
from module.dock_statistics.ship_names import ship_info
from module.logger import logger

REPORT_FOLDER = 'report'
REPORT_CSV = 'ships_report.csv'
REPORT_HTML = 'dock_report.html'

CATEGORY_NAMES = OrderedDict([('dd', '驱逐'), ('cl', '轻巡'), ('ca', '重巡'), ('bb', '战列'), ('cv', '航母'),
                              ('repair', '维修'), ('ss', '潜艇'), ('others', '其他')])
AFFINITY_NAMES = OrderedDict([('oath', '誓约'), ('love', '爱'), ('like', '喜欢'), ('stranger', '陌生'),
                              ('resonance', '共鸣'), ('idol', '应援'), ('unknown', '未知')])
RARITIES = ['普通', '稀有', '精锐', '超稀有', '海上传奇', '最高方案', '决战方案']
# Factions of their own, other factions on wiki are collabs
MAIN_FACTIONS = ['重樱', '白鹰', '皇家', '铁血', '北方联合', '撒丁帝国', '东煌', '自由鸢尾', '维希教廷', '飓风', '郁金王国']
TIERS = [('白', '白（普通）'), ('蓝', '蓝（稀有）'), ('紫', '紫（精锐）'), ('金', '金（超稀有、最高方案）'),
         ('彩', '彩（海上传奇、决战方案）')]
COLUMNS = ['id', 'category', 'name', 'retrofit', 'faction', 'faction_group', 'hull', 'rarity', 'cost_tier', 'level',
           'exp', 'level_cap', 'stars', 'low_cost', 'affinity', 'affinity_value', 'oath_date', 'skills',
           'skills_maxed', 'chips_to_120', 'chips_to_125', 'chips2_to_125']

# Chart ink and series colors, light only, the page is a document to read or print
INK, INK2, MUTED, GRID, AXIS = '#0b0b0b', '#52514e', '#898781', '#e1e0d9', '#c3c2b7'
BLUE = '#2a78d6'
# Ordinal ramp from light to dark, the lightest step still stands out on white
RAMP3 = ['#86b6ef', '#2a78d6', '#104281']
FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif'
# Chart width in SVG units, charts scale with the page
W = 680


# ---------- Data ----------

def faction_group(faction: str) -> str:
    if not faction:
        return '未知'
    if faction.startswith('META'):
        return 'META'
    if faction in MAIN_FACTIONS or faction == '其他':
        return faction
    return '联动'


def build_rows(folder) -> List[Dict]:
    """
    Read ships.jsonl of a run and join it with wiki info and awakening costs.
    """
    rows = []
    with open(os.path.join(folder, 'ships.jsonl'), encoding='utf-8') as f:
        for line in f:
            ship = json.loads(line)
            info = ship_info(ship['name'], ship['category']) or {}
            faction = info.get('faction', '')
            if faction.startswith('META'):
                faction = 'META'
            rarity = info.get('rarity', '')
            learned = [s for s in ship['skills'] if s]
            cap = level_cap(ship['level'], ship['exp'])
            tier = cost_tier(rarity, ship['stars'])
            row = {
                'id': f'{ship["category"]}_{ship["index"]:03d}',
                'category': CATEGORY_NAMES.get(ship['category'], ship['category']),
                'name': ship['name'],
                'retrofit': int(bool(re.search(r'[.·]改$', ship['name']))),
                'faction': faction,
                'faction_group': faction_group(faction),
                'hull': info.get('type', ''),
                'rarity': rarity,
                'cost_tier': tier,
                'level': ship['level'],
                'exp': ship['exp'],
                'level_cap': cap,
                'stars': ship['stars'],
                'low_cost': int(is_low_cost(ship['level'], ship['stars'], rarity)),
                'affinity': AFFINITY_NAMES.get(ship['affinity'], ship['affinity']),
                'affinity_value': ship['affinity_value'],
                'oath_date': ship['oath_date'],
                'skills': '/'.join(str(s) for s in learned),
                'skills_maxed': int(bool(learned) and all(s == 10 for s in learned)),
            }
            row.update(awakening_cost(tier, cap))
            # For the page only
            row['skill_list'] = learned
            if row['skills_maxed']:
                row['skill_state'] = 'maxed'
            elif len(learned) == 3 and learned[0] == 10 and learned[1] == 10:
                row['skill_state'] = 'third'
            else:
                row['skill_state'] = 'other'
            rows.append(row)
    return rows


def write_csv(rows, file):
    # utf-8-sig lets Excel open Chinese names correctly
    with open(file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(['' if row[c] is None else row[c] for c in COLUMNS])


# ---------- SVG charts ----------

def esc(text):
    return html.escape(str(text))


def text_width(text, size):
    # CJK glyphs are one em wide, latin and digits about 0.6 em
    return sum(size if ord(c) > 0x2e80 else size * 0.6 for c in str(text))


def pct(a, b):
    return f'{a / b:.0%}' if b else '—'


def fmt_number(value):
    return f'{value:,}'


def hbar_path(x, y, w, h, r=4):
    """Bar growing right, square at the baseline, rounded data end."""
    if w <= 0:
        return ''
    r = min(r, w, h / 2)
    return (f'M{x:.1f},{y:.1f} H{x + w - r:.1f} A{r},{r} 0 0 1 {x + w:.1f},{y + r:.1f} '
            f'V{y + h - r:.1f} A{r},{r} 0 0 1 {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z')


def col_path(x, y, w, h, r=4):
    """Column growing up from the baseline at y + h, rounded data end."""
    if h <= 0:
        return ''
    r = min(r, h, w / 2)
    return (f'M{x:.1f},{y + h:.1f} V{y + r:.1f} A{r},{r} 0 0 1 {x + r:.1f},{y:.1f} '
            f'H{x + w - r:.1f} A{r},{r} 0 0 1 {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z')


def svg(height, body, label):
    return (f'<svg class="chart" viewBox="0 0 {W} {height:.0f}" width="100%" role="img" aria-label="{esc(label)}" '
            f'font-family=\'{FONT}\' xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def legend(items, y=12):
    """One row of swatches with names, left aligned."""
    out, x = [], 0
    for name, color in items:
        out.append(f'<rect x="{x:.1f}" y="{y - 9}" width="10" height="10" rx="2" fill="{color}"/>'
                   f'<text x="{x + 15:.1f}" y="{y}" font-size="12" fill="{INK2}">{esc(name)}</text>')
        x += 15 + text_width(name, 12) + 18
    return ''.join(out)


def bar_label(y, bar_h, name, left):
    return (f'<text x="{left - 10}" y="{y + bar_h / 2 + 4.5:.1f}" font-size="13" fill="{INK}" '
            f'text-anchor="end">{esc(name)}</text>')


def hbars(items, label, fmt=lambda v, name: str(v), bar_h=18, gap=10):
    """Single series horizontal bars, value at the tip. items: [(name, value)]"""
    left = max(text_width(n, 13) for n, _ in items) + 12
    tip = max(text_width(fmt(v, n), 12) for n, v in items) + 8
    vmax = max(v for _, v in items) or 1
    span = W - left - tip
    body, y = [], 4
    for name, v in items:
        w = span * v / vmax
        body.append(bar_label(y, bar_h, name, left))
        body.append(f'<path d="{hbar_path(left, y, w, bar_h)}" fill="{BLUE}"/>')
        body.append(f'<text x="{left + w + 6:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" fill="{INK2}" '
                    f'style="font-variant-numeric: tabular-nums">{esc(fmt(v, name))}</text>')
        y += bar_h + gap
    body.insert(0, f'<line x1="{left}" x2="{left}" y1="0" y2="{y - gap + 4}" stroke="{AXIS}" stroke-width="1"/>')
    return svg(y - gap + 8, ''.join(body), label)


def stacked_bars(items, names, colors, label, share=True, fmt=fmt_number, bar_h=18, gap=12):
    """
    Stacked horizontal bars with a legend, 2px surface gap between segments.

    Args:
        items: [(name, [values])]
        share: True to fill each bar to 100% with shares inside and totals on the right,
            False to put bars on one shared scale with totals at the tip
    """
    left = max(text_width(n, 13) for n, _ in items) + 12
    totals = [sum(v) for _, v in items]
    right = max(text_width(fmt(t), 12) for t in totals) + 14
    span = W - left - right
    vmax = max(totals) or 1
    body = [legend(list(zip(names, colors)))]
    y = 26
    for (name, values), total in zip(items, totals):
        body.append(bar_label(y, bar_h, name, left))
        x = left
        last = max((i for i, v in enumerate(values) if v), default=-1)
        for i, (v, color) in enumerate(zip(values, colors)):
            if not v:
                continue
            w = span * v / (total if share else vmax)
            seg_w = w - (2 if i < last else 0)
            if i == last:
                body.append(f'<path d="{hbar_path(x, y, seg_w, bar_h)}" fill="{color}"/>')
            else:
                body.append(f'<rect x="{x:.1f}" y="{y}" width="{max(seg_w, 0):.1f}" height="{bar_h}" fill="{color}"/>')
            # Share inside the segment only when it fits with padding
            text = f'{v / total:.0%}'
            if share and seg_w >= text_width(text, 11) + 10:
                ink = INK if color == RAMP3[0] else '#ffffff'
                body.append(f'<text x="{x + 5:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="11" fill="{ink}" '
                            f'style="font-variant-numeric: tabular-nums">{text}</text>')
            x += w
        tip = W - right + 8 if share else x + 6
        body.append(f'<text x="{tip:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" fill="{MUTED if share else INK2}" '
                    f'style="font-variant-numeric: tabular-nums">{esc(fmt(total))}</text>')
        y += bar_h + gap
    if not share:
        body.insert(1, f'<line x1="{left}" x2="{left}" y1="22" y2="{y - gap + 4}" stroke="{AXIS}" stroke-width="1"/>')
    return svg(y - gap + 8, ''.join(body), label)


def columns(items, label, height=200):
    """Single series columns, value on the cap. items: [(name, value)]"""
    top, bottom = 22, 28
    band = W / len(items)
    col_w = min(36, band * 0.6)
    vmax = max(v for _, v in items) or 1
    plot_h = height - top - bottom
    body = [f'<line x1="0" x2="{W}" y1="{top + plot_h}" y2="{top + plot_h}" stroke="{AXIS}" stroke-width="1"/>']
    for i, (name, v) in enumerate(items):
        cx = band * i + band / 2
        h = plot_h * v / vmax
        body.append(f'<path d="{col_path(cx - col_w / 2, top + plot_h - h, col_w, h)}" fill="{BLUE}"/>')
        body.append(f'<text x="{cx:.1f}" y="{top + plot_h - h - 6:.1f}" font-size="12" fill="{INK2}" '
                    f'text-anchor="middle" style="font-variant-numeric: tabular-nums">{esc(v)}</text>')
        body.append(f'<text x="{cx:.1f}" y="{height - 8}" font-size="12" fill="{INK2}" '
                    f'text-anchor="middle">{esc(name)}</text>')
    return svg(height, ''.join(body), label)


# ---------- Tables ----------

def table(head, body_rows, classes='', total_last=False):
    """
    Args:
        head: [(title, is_number)]
        body_rows: [[cell]], cells in the same order as head
    """
    cls = lambda num: ' class="num"' if num else ''
    thead = ''.join(f'<th{cls(num)}>{esc(t)}</th>' for t, num in head)
    rows = []
    for i, cells in enumerate(body_rows):
        tr = ' class="total"' if total_last and i == len(body_rows) - 1 else ''
        tds = ''.join(f'<td{cls(num)}>{esc(c)}</td>' for c, (_, num) in zip(cells, head))
        rows.append(f'<tr{tr}>{tds}</tr>')
    return (f'<div class="table-wrap"><table class="{classes}"><thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


# ---------- Page ----------

CSS = '''
  :root {
    --paper: #ffffff; --ink: %(ink)s; --ink-2: %(ink2)s; --muted: %(muted)s; --rule: %(grid)s; --accent: %(blue)s;
    --font: %(font)s;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  html, body { background: var(--paper); color: var(--ink); }
  body { margin: 0; font-family: var(--font); font-size: 13.5px; line-height: 1.65; }
  .page { max-width: 760px; margin: 0 auto; padding-block: 32px 48px;
          padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
  header { display: grid; gap: 6px; padding-bottom: 18px; border-bottom: 2px solid var(--ink); }
  .eyebrow { font-size: 12px; letter-spacing: .08em; color: var(--muted); }
  h1 { margin: 0; font-size: 30px; line-height: 1.2; font-weight: 700; text-wrap: balance; }
  .lede { margin: 6px 0 0; font-size: 15px; color: var(--ink-2); max-width: 46em; }
  .lede b { color: var(--ink); }
  .kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; background: var(--rule);
          border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; margin-top: 20px; }
  .kpi { background: var(--paper); padding: 12px 14px; display: grid; gap: 2px; }
  .kpi-label { font-size: 12px; color: var(--ink-2); }
  .kpi-value { font-size: 28px; font-weight: 600; line-height: 1.2; }
  .kpi-note { font-size: 11.5px; color: var(--muted); }
  section { margin-top: 30px; display: grid; gap: 12px; scroll-margin-top: 16px; }
  h2 { margin: 0; font-size: 19px; font-weight: 700; display: flex; gap: 10px; align-items: baseline; }
  h2 .no { font-size: 13px; color: var(--accent); font-weight: 600; }
  section > p { margin: 0; max-width: 46em; color: var(--ink-2); }
  section > p b { color: var(--ink); }
  figure { margin: 0; padding: 14px 16px 12px; border: 1px solid var(--rule); border-radius: 6px; display: grid; gap: 8px; }
  figcaption { display: grid; gap: 1px; }
  figcaption .t { font-size: 14px; font-weight: 600; }
  figcaption .s { font-size: 12px; color: var(--muted); }
  .chart { display: block; max-width: 100%%; height: auto; }
  table { width: 100%%; border-collapse: collapse; font-size: 12.5px; }
  th, td { padding: 5px 8px; text-align: left; border-bottom: 1px solid var(--rule); }
  th { font-weight: 600; color: var(--ink-2); border-bottom-color: var(--ink-2); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.total td { font-weight: 600; border-top: 1px solid var(--ink-2); border-bottom: none; }
  .table-wrap { overflow-x: auto; }
  .list td:nth-child(2) { font-weight: 500; }
  .notes { margin: 0; padding-left: 1.2em; color: var(--ink-2); font-size: 12.5px; display: grid; gap: 4px; }
  /* Table of contents: a block under the figures, fixed in the left margin when the window is wide */
  .toc { margin-top: 20px; padding: 12px 16px; border: 1px solid var(--rule); border-radius: 6px; }
  .toc-title { font-size: 12px; letter-spacing: .08em; color: var(--muted); margin-bottom: 4px; }
  .toc ol { list-style: none; margin: 0; padding: 0; display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 2px 16px; }
  .toc a { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; color: var(--ink-2);
           text-decoration: none; font-size: 13px; }
  .toc a .no { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .toc a:hover { color: var(--accent); }
  .toc a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
  .toc a.active { color: var(--ink); font-weight: 600; }
  .toc a.active .no { color: var(--accent); }
  @media (min-width: 1240px) {
    .toc { position: fixed; top: 32px; left: calc(50%% - 380px - 210px); width: 180px; margin: 0; padding: 0; border: none; }
    .toc ol { grid-template-columns: 1fr; }
    .toc a { padding: 4px 0 4px 10px; border-left: 2px solid var(--rule); }
    .toc a.active { border-left-color: var(--accent); }
  }
  footer { margin-top: 28px; padding-top: 10px; border-top: 1px solid var(--rule); font-size: 11.5px; color: var(--muted); }
  @media (max-width: 520px) { .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } h1 { font-size: 24px; } }
''' % {'ink': INK, 'ink2': INK2, 'muted': MUTED, 'grid': GRID, 'blue': BLUE, 'font': FONT}

SCRIPT = '''
  // Mark the section being read in the table of contents
  const links = new Map([...document.querySelectorAll('.toc a')].map((a) => [a.getAttribute('href').slice(1), a]));
  const seen = new Set();
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((e) => (e.isIntersecting ? seen.add(e.target.id) : seen.delete(e.target.id)));
    const first = [...links.keys()].find((id) => seen.has(id));
    links.forEach((a, id) => a.classList.toggle('active', id === first));
  }, { rootMargin: '0px 0px -60% 0px' });
  document.querySelectorAll('section[id]').forEach((el) => observer.observe(el));
'''

SECTIONS = [('level', '等级'), ('skills', '技能'), ('affinity', '好感与誓约'), ('faction', '阵营与稀有度'),
            ('chips', '认知觉醒所需心智'), ('todo', '待练清单'), ('notes', '数据说明')]


def figure(title, note, chart):
    return (f'<figure><figcaption><span class="t">{esc(title)}</span><span class="s">{esc(note)}</span></figcaption>'
            f'{chart}</figure>')


def section(key, body):
    no = [k for k, _ in SECTIONS].index(key) + 1
    title = dict(SECTIONS)[key]
    return f'<section id="{key}"><h2><span class="no">{no:02d}</span>{esc(title)}</h2>{body}</section>'


def scan_time(folder):
    """Run folders are named by the time the scan started, 20261105_082050"""
    try:
        return datetime.strptime(os.path.basename(os.path.abspath(folder)), '%Y%m%d_%H%M%S')
    except ValueError:
        return None


def render_html(rows: List[Dict], folder) -> str:
    n = len(rows)
    by_cat = OrderedDict((c, [r for r in rows if r['category'] == c]) for c in CATEGORY_NAMES.values())
    by_cat = OrderedDict((c, rs) for c, rs in by_cat.items() if rs)
    lv120 = sum(r['level'] >= 120 for r in rows)
    lv125 = sum(r['level'] == 125 for r in rows)
    oath = [r for r in rows if r['affinity'] == '誓约']
    oath200 = sum(r['affinity_value'] == 200 for r in oath)
    dated = [r for r in oath if r['oath_date']]
    maxed = sum(r['skills_maxed'] for r in rows)
    three = [r for r in rows if len(r['skill_list']) == 3]
    third_lv1 = [r for r in three if r['skill_list'][2] == 1]
    vanguard = ['驱逐', '轻巡', '重巡']
    meta = sum(r['faction_group'] == 'META' for r in rows)
    # Low-cost ships stay at Lv100 on purpose, they are not training targets
    low_cost = [r for r in rows if r['low_cost']]
    targets = [r for r in rows if not r['low_cost']]
    chips120 = sum(r['chips_to_120'] for r in targets)
    chips125 = sum(r['chips_to_125'] for r in targets)
    chips2 = sum(r['chips2_to_125'] for r in targets)
    n120 = sum(r['chips_to_120'] > 0 for r in targets)
    n125 = sum(r['chips_to_125'] > 0 for r in targets)
    try:
        with open(os.path.join(folder, 'summary.json'), encoding='utf-8') as f:
            verify = json.load(f).get('verify', {})
    except FileNotFoundError:
        verify = {}
    started = scan_time(folder)
    day = started.strftime('%Y-%m-%d') if started else ''

    # Headline
    kpis = [
        ('上锁舰船', n, f'另有 {meta} 艘 META 不占船坞' if meta else '已记录的上锁舰船'),
        ('平均等级', f'{sum(r["level"] for r in rows) / n:.1f}', '满级为 125'),
        ('120 级及以上', lv120, f'占 {pct(lv120, n)}'),
        ('125 级', lv125, f'占 {pct(lv125, n)}'),
        ('誓约', len(oath), f'其中 {oath200} 艘好感 200'),
        ('技能全满', maxed, f'占 {pct(maxed, n)}'),
    ]
    kpi_html = ''.join(f'<div class="kpi"><div class="kpi-label">{esc(a)}</div><div class="kpi-value">{esc(b)}</div>'
                       f'<div class="kpi-note">{esc(c)}</div></div>' for a, b, c in kpis)
    toc = ''.join(f'<li><a href="#{k}"><span class="no">{i:02d}</span>{esc(t)}</a></li>'
                  for i, (k, t) in enumerate(SECTIONS, start=1))
    van_third = sum(r['category'] in vanguard for r in third_lv1)
    lede = (f'<b>{pct(lv120, n)}</b> 的上锁舰船已到 120 级以上，{lv125} 艘满 125 级。'
            f'有第三技能的 {len(three)} 艘里，<b>{len(third_lv1)} 艘</b>的第三技能还是 1 级，'
            f'其中驱逐和巡洋舰 {van_third} 艘。'
            f'不算低耗船，全部认知觉醒到 125 还需要 <b>{fmt_number(chips125)}</b> 个心智单元。')

    # 01 Level
    level_bins = [('100 以下', lambda lv: lv < 100), ('100–109', lambda lv: 100 <= lv < 110),
                  ('110–119', lambda lv: 110 <= lv < 120), ('120', lambda lv: lv == 120),
                  ('121–124', lambda lv: 121 <= lv <= 124), ('125', lambda lv: lv == 125)]
    band = lambda lv: 2 if lv == 125 else 1 if lv >= 120 else 0
    below120 = sorted([r for r in targets if r['level'] < 120], key=lambda r: (r['level'], r['id']))
    cat_rows = []
    for c, rs in list(by_cat.items()) + [('合计', rows)]:
        m = sum(r['skills_maxed'] for r in rs)
        cat_rows.append([c, len(rs), f'{sum(r["level"] for r in rs) / len(rs):.1f}',
                         sum(r['level'] == 125 for r in rs), sum(r['affinity'] == '誓约' for r in rs), m,
                         pct(m, len(rs))])
    level = (
        f'<p>{n} 艘里 {lv125} 艘满 125 级，{lv120 - lv125} 艘在 120–124 级。'
        f'120 级以下的 {n - lv120} 艘里，{len(low_cost)} 艘是 100 级、未突破的低耗船，'
        f'其余 {len(below120)} 艘见第 06 节。</p>'
        + figure('各等级段舰船数', f'上锁舰船，共 {n} 艘',
                 columns([(name, sum(f(r['level']) for r in rows)) for name, f in level_bins], '各等级段舰船数'))
        + figure('各舰种等级构成', '按舰种的等级段占比，右侧为舰船数',
                 stacked_bars([(c, [sum(band(r['level']) == b for r in rs) for b in range(3)])
                               for c, rs in by_cat.items()],
                              ['120 级以下', '120–124 级', '125 级'], RAMP3, '各舰种等级构成'))
        + table([('舰种', False), ('上锁', True), ('平均等级', True), ('125 级', True), ('誓约', True),
                 ('技能全满', True), ('全满比例', True)], cat_rows, total_last=True)
    )

    # 02 Skills
    main = [r for r in rows if r['category'] in ('战列', '航母')]
    van = [r for r in rows if r['category'] in vanguard]
    third_by_cat = Counter(r['category'] for r in third_lv1)
    skills = (
        f'<p>技能全满的比例：战列和航母 {sum(r["skills_maxed"] for r in main)}/{len(main)} 艘，'
        f'驱逐和巡洋舰 {sum(r["skills_maxed"] for r in van)}/{len(van)} 艘。'
        f'第三技能停在 1 级的有' + '、'.join(f'{c} {third_by_cat[c]} 艘' for c in vanguard) + '。</p>'
        + figure('各舰种技能完成度', '技能全满 = 已学会的技能都到 10 级',
                 stacked_bars([(c, [sum(r['skill_state'] == s for r in rs) for s in ('other', 'third', 'maxed')])
                               for c, rs in by_cat.items()],
                              ['还有其他技能未满', '只差第三技能', '技能全满'], RAMP3, '各舰种技能完成度'))
    )

    # 03 Affinity
    aff = Counter(r['affinity'] for r in rows)
    years = Counter(r['oath_date'][:4] for r in dated)
    affinity = (f'<p>{len(oath)} 艘已誓约，其中 {oath200} 艘好感 200；{aff["爱"]} 艘为“爱”。'
                + (f'读到誓约日期的 {len(dated)} 艘里，{max(years, key=years.get)} 年最多'
                   f'（{max(years.values())} 艘）。' if years else '')
                + '</p>'
                + figure('好感状态', '“共鸣”为 META 舰船，“应援”为偶像大师联动舰船',
                         hbars([(a, aff[a]) for a in AFFINITY_NAMES.values() if aff[a]], '好感状态分布',
                               fmt=lambda v, name: f'{v}（{v / n:.0%}）')))
    if years:
        span = [str(y) for y in range(int(min(years)), int(max(years)) + 1)]
        affinity += figure('每年誓约数', f'按证书上的誓约日期，{len(dated)}/{len(oath)} 艘可读',
                           columns([(y, years[y]) for y in span], '每年誓约数', height=190))

    # 04 Faction and rarity
    fac = Counter(r['faction_group'] for r in rows)
    fac_125 = Counter(r['faction_group'] for r in rows if r['level'] == 125)
    top = [(f, c) for f, c in fac.most_common() if f not in ('联动', 'META', '未知')][:2]
    rar = Counter(r['rarity'] for r in rows if r['rarity'])
    faction = (
        '<p>' + (('最多的是' + '、'.join(f'{f} {c} 艘' for f, c in top) + '；') if top else '')
        + f'联动舰船 {fac["联动"]} 艘，META {meta} 艘。</p>'
        + figure('各阵营上锁舰船', '联动舰船合为一组，标注为舰船数与其中 125 级的数量',
                 hbars(fac.most_common(), '各阵营上锁舰船数',
                       fmt=lambda v, name: f'{v} 艘 · 125 级 {fac_125[name]}', bar_h=16, gap=8))
        + figure('各稀有度舰船数', '稀有度取自 BWIKI 舰船图鉴，改造舰按改造前计',
                 columns([(k, rar[k]) for k in RARITIES], '各稀有度舰船数', height=190))
    )

    # 05 Cognitive awakening
    tier_rows = []
    for key, name in TIERS:
        rs = [r for r in targets if r['cost_tier'] == key]
        tier_rows.append([name, sum(r['chips_to_125'] > 0 for r in rs), fmt_number(sum(r['chips_to_120'] for r in rs)),
                          fmt_number(sum(r['chips_to_125'] for r in rs)),
                          fmt_number(sum(r['chips2_to_125'] for r in rs))])
    cost_rows = [[name, fmt_number(sum(COST[key][0][:4])), fmt_number(COST[key][0][4]), fmt_number(COST[key][1])]
                 for key, name in TIERS]
    chips = (
        f'<p>不算 {len(low_cost)} 艘低耗船：把还没到 120 级上限的 {n120} 艘全部觉醒到 120，'
        f'需要 <b>{fmt_number(chips120)}</b> 个心智单元；把 {n125} 艘全部觉醒到 125，'
        f'一共需要 <b>{fmt_number(chips125)}</b> 个心智单元和 <b>{fmt_number(chips2)}</b> 个心智单元II，'
        f'其中 120→125 这一步占 {pct(chips125 - chips120, chips125)}。</p>'
        + table([('目标', False), ('需觉醒舰船', True), ('心智单元', True), ('心智单元II', True), ('物资', True)],
                [['全部到 120', n120, fmt_number(chips120), '—', fmt_number(chips120 * 10)],
                 ['全部到 125', n125, fmt_number(chips125), fmt_number(chips2), fmt_number(chips125 * 10)]])
        + figure('各舰种所需心智单元', '全部觉醒到 125，按觉醒阶段拆分',
                 stacked_bars([(c, [sum(r['chips_to_120'] for r in rs if not r['low_cost']),
                                    sum(r['chips_to_125'] - r['chips_to_120'] for r in rs if not r['low_cost'])])
                               for c, rs in by_cat.items()],
                              ['觉醒到 120', '120→125'], [RAMP3[2], RAMP3[0]], '各舰种所需心智单元', share=False))
        + table([('品质', False), ('需觉醒舰船', True), ('到 120', True), ('到 125', True), ('心智单元II', True)],
                tier_rows)
        + '<p>单艘消耗（BWIKI 认知觉醒），按改造前的稀有度计。</p>'
        + table([('品质', False), ('100→120 心智单元', True), ('120→125 心智单元', True),
                 ('120→125 心智单元II', True)], cost_rows)
    )

    # 06 Training list
    low_aff = sorted([r for r in targets if r['affinity'] in ('陌生', '喜欢')], key=lambda r: r['affinity_value'] or 0)
    todo = (
        f'<p>120 级以下的 {len(below120)} 艘，不含低耗船，按等级排序。</p>'
        + table([('舰种', False), ('舰船', False), ('等级', True), ('好感', False), ('技能', False)],
                [[r['category'], r['name'], r['level'], r['affinity'], r['skills'].replace('/', ' / ') or '—']
                 for r in below120], classes='list')
        + f'<p>好感还没到“爱”的 {len(low_aff)} 艘，不含低耗船。</p>'
        + table([('舰种', False), ('舰船', False), ('等级', True), ('好感', False), ('好感度', True)],
                [[r['category'], r['name'], r['level'], r['affinity'], r['affinity_value']] for r in low_aff],
                classes='list')
    )

    # 07 Notes
    if verify.get('dock_amount'):
        amount = (f'船坞显示数量不含 META 舰船：本次扫描 {verify["scanned"]} 艘，减去 {verify["meta"]} 艘 META 为 '
                  f'{verify["scanned"] - verify["meta"]}，船坞显示 {verify["dock_amount"]}，'
                  + ('数量核对一致。' if verify.get('amount_match') else '数量对不上，可能有漏扫。'))
    else:
        amount = '本次不是全量扫描，没有核对船坞数量。'
    notes = (
        '<ul class="notes">'
        f'<li>数据来自 ALAS 船坞扫描，逐艘打开详情页和誓约证书读取，只记录上锁舰船'
        + (f'；扫描开始于 {started:%Y-%m-%d %H:%M}' if started else '')
        + f'。截图存档在 {esc(os.path.basename(os.path.abspath(folder)))}\\images。</li>'
        f'<li>{amount}</li>'
        '<li>船名先经 OCR，再按 BWIKI 舰船图鉴的官方名校正。阵营与稀有度也取自 BWIKI。</li>'
        f'<li>META 舰船显示“共鸣”，没有读取好感数值；誓约日期只能从部分证书版式读到（{len(dated)}/{len(oath)}）。</li>'
        f'<li>低耗船：100 级且星数仍是初始星数（未突破）的舰船，共 {len(low_cost)} 艘，不计入待练清单和心智统计。</li>'
        '<li>心智消耗取自 BWIKI 认知觉醒：按改造前稀有度计，最高方案按金船、决战方案按彩船计；'
        'BWIKI 未收录的联动舰船按满星星数推定品质。当前等级上限按经验判断：经验显示 MAX 的船已到上限，'
        '其余按正在进行的阶段计。物资为心智单元的 10 倍。</li>'
        f'<li>完整明细见同目录的 {REPORT_CSV}，每艘一行，含等级上限、是否低耗船和所需心智。</li>'
        '</ul>'
    )

    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>船坞练度报告</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
<header>
  <div class="eyebrow">碧蓝航线 · 国服 · 船坞扫描 {day}</div>
  <h1>船坞练度报告</h1>
  <p class="lede">{lede}</p>
</header>
<div class="kpis">{kpi_html}</div>
<nav class="toc" aria-label="目录"><div class="toc-title">目录</div><ol>{toc}</ol></nav>
{section('level', level)}
{section('skills', skills)}
{section('affinity', affinity)}
{section('faction', faction)}
{section('chips', chips)}
{section('todo', todo)}
{section('notes', notes)}
<footer>生成于 {datetime.now():%Y-%m-%d %H:%M} · 数据来源：ALAS dock statistics</footer>
</div>
<script>{SCRIPT}</script>
</body>
</html>
'''


def generate_report(folder) -> str:
    """
    Args:
        folder (str): Output folder of a run, like ../AzurLaneDock/20261105_082050

    Returns:
        str: Path of the HTML report, or '' if the run recorded no ships
    """
    rows = build_rows(folder)
    if not rows:
        logger.warning('No ships recorded, report skipped')
        return ''
    out = os.path.join(folder, REPORT_FOLDER)
    os.makedirs(out, exist_ok=True)
    write_csv(rows, os.path.join(out, REPORT_CSV))
    file = os.path.abspath(os.path.join(out, REPORT_HTML))
    with open(file, 'w', encoding='utf-8') as f:
        f.write(render_html(rows, folder))
    logger.attr('Report', file)
    return file
