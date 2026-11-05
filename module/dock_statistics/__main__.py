"""
Scan ship detail pages in dock and save training statistics of locked ships.

Usage, at ALAS root folder:
    python -m module.dock_statistics
    python -m module.dock_statistics --config alas --category dd cl --limit 5
    python -m module.dock_statistics --reread ../AzurLaneDock/20261105_101010
    python -m module.dock_statistics --update-names
"""
import argparse

from module.dock_statistics.dock_statistics import CATEGORIES, DEFAULT_OUTPUT, DockStatistics

parser = argparse.ArgumentParser(description='ALAS dock statistics')
parser.add_argument('--config', default='alas', help='Config name under ./config, default to alas')
parser.add_argument('--category', nargs='+', choices=CATEGORIES, default=None,
                    help='Dock filter indexes to scan, default to all')
parser.add_argument('--limit', type=int, default=0, help='Max ships to go through per category, 0 for all')
parser.add_argument('--ascending', action='store_true', help='Scan from the lowest level, default to highest first')
parser.add_argument('--output', default=DEFAULT_OUTPUT,
                    help='Folder to save results, default to AzurLaneDock next to ALAS folder')
parser.add_argument('--reread', default=None, metavar='FOLDER',
                    help='Read results again from images archived in a previous run, no emulator needed')
parser.add_argument('--start-game', action='store_true',
                    help='Restart the game and login first, retry every 20min for 6 times if server is down')
parser.add_argument('--update-names', action='store_true',
                    help='Download official ship names from BWIKI, used to fix ship names from OCR')
args = parser.parse_args()

if args.update_names:
    from module.dock_statistics.ship_names import update_ship_names

    print(f'{len(update_ship_names())} ship names saved')
elif args.reread:
    DockStatistics.reread(args.reread)
else:
    DockStatistics(config=args.config, task='Alas').run(
        categories=args.category, limit=args.limit, ascending=args.ascending, output=args.output,
        start_game=args.start_game)
