import csv
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional

import cv2
import numpy as np

from module.base.timer import Timer
from module.base.utils import crop, load_image, random_rectangle_point, save_image
from module.dock_statistics.detail import (AFFINITY_ICON, AFFINITY_PAGE_CLOSE, AFFINITY_ROW_SEARCH_AREA,
                                           DETAIL_STABLE_AREAS, OCR_LEVEL, ShipDetail, affinity_page_appear,
                                           classify_affinity, detail_locked, parse_level, scan_affinity_page,
                                           scan_detail)
from module.dock_statistics.report import generate_report
from module.dock_statistics.ship_names import match_ship_name
from module.exception import GameBugError, GameNotRunningError, GamePageUnknownError, GameStuckError, \
    GameTooManyClickError
from module.handler.login import LoginHandler
from module.logger import logger
from module.retire.assets import DOCK_CHECK, SHIP_DETAIL_CHECK
from module.retire.dock import Dock
from module.retire.enhancement import OCR_DOCK_AMOUNT
from module.retire.scanner import LevelScanner
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock

# Dock filter `index` options that partition the whole dock
CATEGORIES = ['dd', 'cl', 'ca', 'bb', 'cv', 'repair', 'ss', 'others']
# Areas to detect ship switching on detail page, and the mean pixel difference that counts as switched.
# Painting area stops above the dialogue bubble, which pops up by itself.
# Identical ships only differ during the sliding animation, painting difference peaks at about 40.
SWITCH_AREAS = {
    'name': ((140, 60, 480, 140), 5),
    'panel': ((690, 220, 1250, 610), 5),
    'painting': ((110, 140, 680, 460), 15),
}
# Stop if the same ship shows this many times in a row, in case a moving painting fakes a ship switch
MAX_IDENTICAL_SHIPS = 30
# Name, stars, level and exp, to tell if two consecutive ships look the same
SIGNATURE_AREA = (140, 60, 1250, 320)
# Swipe horizontally near the bottom of the painting.
# Swiping with Equipment.SWIPE_AREA, which covers the upper body, sometimes leaves the page
# refusing to switch ships until it's re-entered.
SWIPE_START_AREA = (520, 590, 540, 620)
SWIPE_VECTOR = (-250, 0)
# Results are kept as an archive, outside ALAS folder so they survive reinstalling ALAS.
# Relative to ALAS root, a sibling folder of it.
DEFAULT_OUTPUT = '../AzurLaneDock'
# Full screenshots of every recorded ship, about 1.2MB each, 2GB for a whole dock.
# Certificate is missing if affinity page is not opened, such as META ships.
ARCHIVE_FOLDER = 'images'
ARCHIVE_DETAIL = '{category}_{index:03d}_detail.png'
ARCHIVE_AFFINITY = '{category}_{index:03d}_affinity.png'
# Fields corrected by hand, applied when re-reading
MANUAL_FILE = 'manual.json'


def _area_diff(image1, image2, area):
    return float(np.mean(np.abs(crop(image1, area).astype(np.int16) - crop(image2, area).astype(np.int16))))


def _signature(image):
    return cv2.resize(crop(image, SIGNATURE_AREA), (111, 26), interpolation=cv2.INTER_AREA).astype(np.int16)


class DockStatistics(Dock):
    output_dir = ''
    _sampled = set()
    _archive: Optional[ThreadPoolExecutor] = None
    # Minimum time between a ship switch and the next swipe
    _switch_interval = Timer(1.2)

    def _detail_wait_stable(self, areas=None, timeout=3):
        """
        Wait until areas stop changing, as panels slide in after a swipe.

        Args:
            areas (list[tuple]): Default to DETAIL_STABLE_AREAS
            timeout (int, float):

        Returns:
            bool: True if stable, False if still changing after timeout

        Pages:
            in: SHIP_DETAIL_CHECK
        """
        areas = areas or DETAIL_STABLE_AREAS
        prev = None
        for _ in self.loop(timeout=timeout):
            image = self.device.image.copy()
            if prev is not None and all(_area_diff(prev, image, area) < 1 for area in areas):
                return True
            prev = image
            self.device.sleep(0.2)
        logger.warning(f'Ship detail is still changing after {timeout}s')
        return False

    def _save_sample(self, name, problems):
        """
        Save screenshot once per sample name, for later calibration.
        """
        if name in self._sampled or not self.output_dir:
            return
        self._sampled.add(name)
        file = os.path.join(self.output_dir, 'samples', f'{name}.png')
        os.makedirs(os.path.dirname(file), exist_ok=True)
        save_image(self.device.image, file)
        logger.info(f'Sample saved: {file} {problems}')

    def _archive_image(self, name):
        """
        Keep current screenshot in the images folder, so results can be re-read after parsers improve.
        Images are saved in background, PNG encoding takes about 0.3s.
        """
        if not self.output_dir or self._archive is None:
            return
        file = os.path.join(self.output_dir, ARCHIVE_FOLDER, name)
        self._archive.submit(save_image, self.device.image.copy(), file)

    def detail_scan(self, category, index) -> ShipDetail:
        """
        Pages:
            in: SHIP_DETAIL_CHECK, detail tab
        """
        self._detail_wait_stable()
        detail = scan_detail(self.device.image, category=category, index=index)
        if detail.affinity == 'unknown':
            # Affinity icon pops in after switching ships
            self.device.sleep(0.5)
            self.device.screenshot()
            detail.affinity = classify_affinity(self.device.image)
            if detail.affinity == 'unknown':
                self._save_sample(f'affinity_unknown_{category}_{index}', ['affinity'])
        self._archive_image(ARCHIVE_DETAIL.format(category=category, index=index))
        return detail

    def affinity_page_read(self, detail: ShipDetail):
        """
        Tap the affinity icon, read affinity value and name from the marriage certificate, then close it.
        The name on certificate is more reliable, it replaces the one from detail page if recognized.

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        clicked = False
        for _ in self.loop(timeout=5):
            # Certificate fades in, ship detail is still visible at first
            if affinity_page_appear(self.device.image) and not self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                break
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=2):
                self.device.click(AFFINITY_ICON)
                clicked = True
                continue
        else:
            self._save_sample(f'affinity_page_{detail.category}_{detail.index}', ['affinity_page'])
            if clicked and not self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                logger.warning('Unknown page after tapping affinity icon')
                # Probably a certificate that is not recognized yet, archive it for re-reading
                self._archive_image(ARCHIVE_AFFINITY.format(category=detail.category, index=detail.index))
                self.affinity_page_close()
            else:
                logger.warning('Affinity page not opened')
            return detail

        # Wait text to fade in
        self._detail_wait_stable(areas=[AFFINITY_ROW_SEARCH_AREA], timeout=2)
        self._archive_image(ARCHIVE_AFFINITY.format(category=detail.category, index=detail.index))
        value, name, oath_date = scan_affinity_page(self.device.image)
        detail.apply_affinity_page(value, name, oath_date)
        if value is None:
            self._save_sample(f'affinity_value_{detail.category}_{detail.index}', ['affinity_value'])

        self.affinity_page_close()
        return detail

    def affinity_page_close(self, skip_first_screenshot=True):
        """
        Tap outside the certificate until ship detail shows steadily.
        A tap during the fade-in animation is ignored, so check again after closed.

        Pages:
            in: affinity page
            out: SHIP_DETAIL_CHECK
        """
        interval = Timer(2)
        confirm = Timer(0.6, count=2)
        tried = 0
        for _ in self.loop(skip_first=skip_first_screenshot, timeout=20):
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)) and not affinity_page_appear(self.device.image):
                if not confirm.started():
                    confirm.start()
                if confirm.reached():
                    break
                continue
            confirm.clear()
            if interval.reached():
                # Rotate close positions, then fall back to the back key
                if tried < 2 * len(AFFINITY_PAGE_CLOSE):
                    self.device.click(AFFINITY_PAGE_CLOSE[tried % len(AFFINITY_PAGE_CLOSE)])
                else:
                    logger.info('Close affinity page with back key')
                    self.device.adb_shell(['input', 'keyevent', '4'])
                tried += 1
                interval.reset()
                continue
        else:
            logger.warning('Failed to close affinity page')

    def _ship_swipe(self, forward=True, retry=3, timeout=1.5):
        """
        Swipe to the next or previous ship.

        Equipment.ship_view_next() compares screenshots before and after swiping,
        which ends early on identical ships, such as multiple Lv.1 copies for limit break.
        Here we catch the switching animation instead, identical ships still slide in.
        Game ignores swipes right after a page loads, so retry a few times.

        Returns:
            bool: True if switched to another ship

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        # Game ignores swipes while the previous ship is still sliding in,
        # wait until the whole page stops changing.
        # Some paintings are animated and never stop, ignore painting if so.
        dynamic = not self._detail_wait_stable(areas=[(110, 60, 1250, 610)], timeout=3)
        areas = {k: v for k, v in SWITCH_AREAS.items() if not (dynamic and k == 'painting')}
        self._switch_interval.wait()

        # Sliding animation of identical ships lasts less than 0.3s, take screenshots faster to catch it
        self.device.screenshot_interval_set(0.03)
        try:
            for _ in range(retry):
                before = self.device.image.copy()
                x, y = random_rectangle_point(SWIPE_START_AREA)
                if forward:
                    p1, p2 = (x, y), (x + SWIPE_VECTOR[0], y + SWIPE_VECTOR[1])
                else:
                    p1, p2 = (x + SWIPE_VECTOR[0], y + SWIPE_VECTOR[1]), (x, y)
                self.device.swipe(p1, p2, name='SHIP_SWIPE')
                peak = {k: 0. for k in areas}
                for _ in self.loop(timeout=timeout):
                    for k, (area, threshold) in areas.items():
                        diff = _area_diff(before, self.device.image, area)
                        peak[k] = max(peak[k], diff)
                        if diff > threshold:
                            logger.info(f'Ship switched {"forward" if forward else "backward"}, '
                                        f'{k} diff={diff:.1f}')
                            # Swiping hundreds of ships is expected, not a stuck game
                            self.device.click_record_clear()
                            self._switch_interval.reset()
                            return True
                    if self.handle_popup_confirm('DOCK_STATISTICS_SWIPE'):
                        continue
                logger.info(f'Ship not switched, dynamic painting={dynamic}, peak diff: '
                            f'{ {k: round(v, 1) for k, v in peak.items()} }')
            return False
        finally:
            self.device.screenshot_interval_set()

    def detail_next(self, category, index):
        """
        Switch to the next ship, recover if game refuses swiping.

        Sometimes the detail page refuses all swipes in both directions until re-entered,
        cause unknown. A refused swipe must not be taken as the end of dock.

        Args:
            category (str): Dock filter index, to re-enter
            index (int): 1-based position of the current ship, to come back after re-entering

        Returns:
            bool: True if switched to the next ship, False if reached the end

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        if self._ship_swipe(forward=True):
            return True

        # Either at the end, or swiping is refused.
        # At the end, swiping backward still works.
        logger.info('Ship not switched, check if reached the end')
        if self._ship_swipe(forward=False, retry=2):
            if not self._ship_swipe(forward=True, retry=2):
                logger.warning('Failed to come back after swiping backward')
                return False
            return self._ship_swipe(forward=True, retry=2)

        logger.warning('Swiping refused in both directions, switch tab to recover')
        self.ship_side_navbar_ensure(bottom=2)
        self.ship_side_navbar_ensure(bottom=1)
        if self._ship_swipe(forward=True):
            return True
        if self._ship_swipe(forward=False, retry=2):
            self._ship_swipe(forward=True, retry=2)
            return self._ship_swipe(forward=True, retry=2)

        logger.warning(f'Swiping still refused, re-enter and swipe back to #{index}')
        self.detail_exit()
        self.dock_filter_set(index=category)
        if not self.dock_enter_first(non_npc=False):
            return False
        self.ship_side_navbar_ensure(bottom=1)
        for i in range(1, index):
            if not self._ship_swipe(forward=True):
                logger.warning(f'Failed to come back to #{index}, stuck at #{i}')
                return False
        return self._ship_swipe(forward=True)

    def detail_exit(self):
        """
        Pages:
            in: SHIP_DETAIL_CHECK
            out: DOCK_CHECK
        """
        logger.info('Ship detail exit')
        interval = Timer(3)
        for _ in self.loop():
            if self.appear(DOCK_CHECK, offset=(20, 20)):
                break
            if interval.reached() and self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                self.device.click(BACK_ARROW)
                interval.reset()
                continue
            if interval.reached() and affinity_page_appear(self.device.image):
                self.device.click(AFFINITY_PAGE_CLOSE[0])
                interval.reset()
                continue
            if self.handle_game_tips():
                continue
        self.handle_dock_cards_loading()

    def scan_category(self, category, writer, limit=0) -> Dict:
        """
        Go through all ships under a dock filter index on detail pages, record locked ones.

        Args:
            category (str): Dock filter index, such as 'dd'
            writer: Opened jsonl file, each locked ship is written once scanned
            limit (int): Max ships to go through, 0 for all

        Returns:
            dict: Locked ships, and levels of all ships for cross-checking

        Pages:
            in: page_dock
            out: page_dock
        """
        logger.hr(f'Dock statistics {category}', level=1)
        self.dock_filter_set(index=category)
        self.device.screenshot()
        # Levels on the first dock page, to cross-check against detail pages
        list_levels = LevelScanner().scan(self.device.image)

        ships: List[ShipDetail] = []
        seen: List[Dict] = []
        if not self.dock_enter_first(non_npc=False):
            logger.info(f'No ships in {category}')
            return {'ships': ships, 'seen': seen, 'list_levels': []}
        self.ship_side_navbar_ensure(bottom=1)

        identical = 0
        prev_signature = None
        while 1:
            index = len(seen) + 1
            self._detail_wait_stable()
            image = self.device.image
            locked = detail_locked(image)
            level = parse_level(OCR_LEVEL.ocr(image, direct_ocr=False))
            seen.append({'index': index, 'level': level, 'locked': locked})

            signature = _signature(image)
            if prev_signature is not None and np.mean(np.abs(signature - prev_signature)) < 2:
                identical += 1
                if identical >= MAX_IDENTICAL_SHIPS:
                    logger.warning(f'Same ship shows {identical} times in a row, assume reached the end')
                    break
            else:
                identical = 0
            prev_signature = signature

            if locked is False:
                logger.info(f'[{category} #{index}] Lv.{level} not locked, skip')
            else:
                if locked is None:
                    logger.warning('Lock state unknown, record it anyway')
                    self._save_sample(f'lock_unknown_{category}_{index}', ['locked'])
                detail = self.detail_scan(category, index=index)
                # META ships show resonance instead, their affinity page is not handled
                if detail.affinity != 'resonance':
                    self.affinity_page_read(detail)
                detail.name = match_ship_name(detail.name, category)
                problems = detail.problems()
                if problems:
                    self._save_sample(f'problem_{category}_{index}', problems)
                logger.info(f'[{category} #{index}] {detail.name} ({detail.name_en}) Lv.{detail.level} '
                            f'stars={detail.stars} affinity={detail.affinity}:{detail.affinity_value} '
                            f'skills={detail.skills}')
                ships.append(detail)
                writer.write(json.dumps(asdict(detail), ensure_ascii=False) + '\n')
                writer.flush()

            if limit and index >= limit:
                logger.info(f'Reach limit {limit}')
                break
            if not self.detail_next(category, index):
                logger.info(f'Reach the last ship in {category}')
                break

        self.detail_exit()
        return {'ships': ships, 'seen': seen, 'list_levels': list_levels[:len(seen)]}

    @staticmethod
    def verify(results: Dict[str, Dict], dock_amount) -> Dict:
        """
        Cross-check detail pages with dock list and dock amount.
        """
        report = {'dock_amount': dock_amount, 'scanned': 0, 'locked': 0, 'meta': 0, 'categories': {}}
        for category, result in results.items():
            seen = result['seen']
            levels = [s['level'] for s in seen]
            list_mismatch = [
                {'index': i + 1, 'list': lv, 'detail': levels[i]}
                for i, lv in enumerate(result['list_levels'])
                if i < len(levels) and lv != levels[i]
            ]
            # Level order is not checked, game's level sorting is not strict
            report['categories'][category] = {
                'scanned': len(seen),
                'locked': len(result['ships']),
                'lock_unknown': [s['index'] for s in seen if s['locked'] is None],
                'list_mismatch': list_mismatch,
                'problems': {s.index: s.problems() for s in result['ships'] if s.problems()},
            }
            report['scanned'] += len(seen)
            report['locked'] += len(result['ships'])
            # Oathed META ships show the oath icon, tell them by name. "META" in name may be lost in OCR.
            report['meta'] += sum(1 for s in result['ships'] if s.name.endswith('META') or s.affinity == 'resonance')
        if dock_amount:
            # META ships don't take dock space
            report['amount_match'] = report['scanned'] - report['meta'] == dock_amount
        return report

    @staticmethod
    def summarize(ships: List[ShipDetail]) -> Dict:
        """
        Statistics of locked ships.
        """
        def level_bucket(level):
            for lower in [125, 120, 110, 100, 90, 70]:
                if level >= lower:
                    return f'{lower}+'
            return '<70'

        def affinity_bucket(value):
            if value is None:
                return 'unknown'
            for lower in [200, 150, 100, 60]:
                if value >= lower:
                    return f'{lower}+'
            return '<60'

        summary = {'total': len(ships), 'categories': {}}
        for category in CATEGORIES + ['all']:
            group = ships if category == 'all' else [s for s in ships if s.category == category]
            if not group:
                continue
            levels = [s.level for s in group]
            values = [s.affinity_value for s in group if s.affinity_value is not None]
            summary['categories'][category] = {
                'count': len(group),
                'level_avg': round(sum(levels) / len(levels), 1),
                'level_buckets': dict(Counter(level_bucket(lv) for lv in levels)),
                'stars': dict(Counter(s.stars for s in group)),
                'affinity': dict(Counter(s.affinity for s in group)),
                'affinity_value_avg': round(sum(values) / len(values), 1) if values else None,
                'affinity_value_buckets': dict(Counter(affinity_bucket(s.affinity_value) for s in group)),
                'skills_maxed': sum(1 for s in group if s.skills and all(lv == 10 for lv in s.skills if lv)),
            }
        return summary

    @staticmethod
    def write_csv(ships: List[ShipDetail], file):
        header = ['category', 'index', 'name', 'name_en', 'level', 'exp', 'stars', 'affinity', 'affinity_value',
                  'oath_date', 'skill_1', 'skill_2', 'skill_3']
        # utf-8-sig lets Excel open Chinese names correctly
        with open(file, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for s in ships:
                row = [s.category, s.index, s.name, s.name_en, s.level, s.exp, s.stars, s.affinity,
                       s.affinity_value, s.oath_date]
                row += (s.skills + ['', '', ''])[:3]
                writer.writerow(row)

    def game_start(self, retry=6, interval=1200):
        """
        Restart the game and login. Retry in case the server is still under maintenance,
        login keeps clicking the maintenance announcement until GameTooManyClickError.

        Args:
            retry (int): Max attempts
            interval (int, float): Seconds to wait between attempts

        Pages:
            in: Any
            out: page_main
        """
        login = LoginHandler(config=self.config, device=self.device)
        for attempt in range(1, retry + 1):
            logger.hr(f'Game start {attempt}/{retry}', level=1)
            try:
                login.app_stop()
                login.app_start()
                return
            except (GameStuckError, GameTooManyClickError, GameNotRunningError, GamePageUnknownError,
                    GameBugError) as e:
                logger.warning(f'Game start failed: {type(e).__name__}')
                if attempt == retry:
                    raise
                logger.info(f'Retry in {interval}s')
                self.device.sleep(interval)

    def run(self, categories=None, limit=0, ascending=False, output=DEFAULT_OUTPUT, start_game=False):
        """
        Args:
            categories (list[str]): Dock filter indexes to scan, default to all
            limit (int): Max ships to go through per category, 0 for all
            ascending (bool): Scan from the lowest level, default to highest first
            output (str): Folder to save results, each run creates a sub folder
            start_game (bool): Restart the game and login first, for unattended runs

        Pages:
            in: Any
            out: page_dock
        """
        if start_game:
            self.game_start()
        categories = categories or CATEGORIES
        self._sampled = set()
        self.output_dir = os.path.abspath(os.path.join(output, datetime.now().strftime('%Y%m%d_%H%M%S')))
        os.makedirs(os.path.join(self.output_dir, ARCHIVE_FOLDER), exist_ok=True)
        logger.hr('Dock statistics', level=0)
        logger.attr('Output', self.output_dir)

        # A previous run may stop on the affinity page, which is unknown to ui_ensure()
        self.device.screenshot()
        if affinity_page_appear(self.device.image):
            self.affinity_page_close()
        self.ui_ensure(page_dock)
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(not ascending, wait_loading=False)
        self.dock_filter_set()
        current, _, total = OCR_DOCK_AMOUNT.ocr(self.device.image)
        logger.attr('Dock amount', f'{current}/{total}')
        # Dock amount only matches when the whole dock is scanned
        full_scan = not limit and set(categories) == set(CATEGORIES)
        dock_amount = current if full_scan else 0

        results = {}
        self._archive = ThreadPoolExecutor(max_workers=1)
        try:
            with open(os.path.join(self.output_dir, 'ships.jsonl'), 'w', encoding='utf-8') as writer:
                for category in categories:
                    results[category] = self.scan_category(category, writer=writer, limit=limit)
                    self.write_scan_record(results, dock_amount, self.output_dir)
        finally:
            # Wait until all images are saved
            self._archive.shutdown(wait=True)
            self._archive = None

        summary = self.save_results(results, dock_amount, self.output_dir)
        self.write_report(self.output_dir)
        self.dock_reset()
        return summary

    @staticmethod
    def write_report(folder):
        """
        Training report under folder/report. It doesn't fail the scan, run --report again after fixing.
        """
        try:
            generate_report(folder)
        except Exception as e:
            logger.exception(e)
            logger.warning('Failed to write report')

    @staticmethod
    def write_scan_record(results: Dict[str, Dict], dock_amount, folder):
        """
        Save all ships gone through, including unlocked ones, so verify() can run again on re-reading.
        """
        record = {
            'dock_amount': dock_amount,
            'categories': {category: {'seen': result['seen'], 'list_levels': result['list_levels']}
                           for category, result in results.items()},
        }
        with open(os.path.join(folder, 'scan.json'), 'w', encoding='utf-8') as f:
            # Levels may be numpy integers
            json.dump(record, f, ensure_ascii=False, indent=2, default=int)

    @staticmethod
    def reread(folder):
        """
        Read ships again from archived images, after parsers are improved.
        ships.jsonl, ships.csv and summary.json in the folder are overwritten.
        Fields corrected by hand in manual.json are applied after reading, like {"dd_038": {"name": "枨"}}.

        Args:
            folder (str): Output folder of a previous run, like ../AzurLaneDock/20261105_101010
        """
        logger.hr('Dock statistics reread', level=0)
        file = os.path.join(folder, 'scan.json')
        if os.path.exists(file):
            with open(file, encoding='utf-8') as f:
                record = json.load(f)
        else:
            logger.warning('No scan.json, verify with archived images only')
            record = {'dock_amount': 0, 'categories': {}}
        file = os.path.join(folder, MANUAL_FILE)
        manual = {}
        if os.path.exists(file):
            with open(file, encoding='utf-8') as f:
                manual = json.load(f)

        def image(name, category, index):
            file = os.path.join(folder, ARCHIVE_FOLDER, name.format(category=category, index=index))
            return load_image(file) if os.path.exists(file) else None

        results = {}
        for category in CATEGORIES:
            indexes = sorted(int(name.split('_')[1]) for name in os.listdir(os.path.join(folder, ARCHIVE_FOLDER))
                             if name.startswith(f'{category}_') and name.endswith('_detail.png'))
            if not indexes:
                continue
            ships = []
            for index in indexes:
                detail = scan_detail(image(ARCHIVE_DETAIL, category, index), category=category, index=index)
                affinity = image(ARCHIVE_AFFINITY, category, index)
                if affinity is not None:
                    detail.apply_affinity_page(*scan_affinity_page(affinity))
                detail.name = match_ship_name(detail.name, category)
                for key, value in manual.get(f'{category}_{index:03d}', {}).items():
                    if not hasattr(detail, key):
                        logger.warning(f'Unknown field in {MANUAL_FILE}: {category}_{index:03d}.{key}')
                        continue
                    logger.info(f'Manual {key}: {getattr(detail, key)} -> {value}')
                    setattr(detail, key, value)
                logger.info(f'[{category} #{index}] {detail.name} Lv.{detail.level} '
                            f'affinity={detail.affinity}:{detail.affinity_value}')
                ships.append(detail)
            # Run stopped in this category, only ships with images are known
            data = record['categories'].get(category, {
                'seen': [{'index': s.index, 'level': s.level, 'locked': True} for s in ships],
                'list_levels': [],
            })
            results[category] = {'ships': ships, **data}

        with open(os.path.join(folder, 'ships.jsonl'), 'w', encoding='utf-8') as writer:
            for result in results.values():
                for detail in result['ships']:
                    writer.write(json.dumps(asdict(detail), ensure_ascii=False) + '\n')
        summary = DockStatistics.save_results(results, record['dock_amount'], folder)
        DockStatistics.write_report(folder)
        return summary

    @staticmethod
    def save_results(results: Dict[str, Dict], dock_amount, folder) -> Dict:
        """
        Write ships.csv and summary.json, and log the results.
        """
        ships = [s for result in results.values() for s in result['ships']]
        DockStatistics.write_csv(ships, os.path.join(folder, 'ships.csv'))
        report = DockStatistics.verify(results, dock_amount=dock_amount)
        summary = DockStatistics.summarize(ships)
        with open(os.path.join(folder, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump({'summary': summary, 'verify': report}, f, ensure_ascii=False, indent=2)

        logger.hr('Dock statistics result', level=1)
        for category, data in report['categories'].items():
            logger.info(f'{category:7s} scanned={data["scanned"]:4d} locked={data["locked"]:4d}')
        for category, data in summary['categories'].items():
            logger.info(f'{category:7s} count={data["count"]:4d} avg_lv={data["level_avg"]:6.1f} '
                        f'levels={data["level_buckets"]} affinity={data["affinity_value_buckets"]}')
        for category, data in report['categories'].items():
            if data['list_mismatch'] or data['problems'] or data['lock_unknown']:
                logger.warning(f'{category}: list_mismatch={data["list_mismatch"]} '
                               f'lock_unknown={data["lock_unknown"]} problems={data["problems"]}')
        if 'amount_match' in report:
            logger.attr('Scanned - META / dock amount',
                        f'{report["scanned"]}-{report["meta"]}/{report["dock_amount"]}')
        logger.attr('Output', folder)
        return summary
