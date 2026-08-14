"""Feedback CLI — rate briefed articles to teach the ranker your taste.

Article IDs are the numbers shown in the brief as [[123]](...).

Usage:
    python -m src.feedback 123 --up
    python -m src.feedback 123 456 --down --note "clickbait"
    python -m src.feedback --stats

Feedback nudges future ranking: sources whose articles you consistently
upvote get up to +10 score, consistently downvoted sources up to -10.
"""

from __future__ import annotations

import argparse
import sys

from . import db
from .config import DB_PATH


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.feedback",
        description="Rate briefed articles (+1/-1) or show feedback stats.",
    )
    parser.add_argument("article_ids", nargs="*", type=int,
                        help="article IDs from the brief, e.g. 123")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--up", action="store_true", help="rate +1")
    group.add_argument("--down", action="store_true", help="rate -1")
    parser.add_argument("--note", default="", help="optional note")
    parser.add_argument("--stats", action="store_true",
                        help="show per-source feedback statistics")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = db.connect(DB_PATH)
    try:
        if args.stats:
            rows = db.feedback_stats(conn)
            if not rows:
                print("No feedback recorded yet.")
                return 0
            print(f"{'source':<28} {'n':>3} {'avg':>6} {'up':>3} {'down':>4}   adjustment")
            adjustments = db.source_feedback_adjustments(conn)
            id_by_name = {
                r["name"]: r["id"]
                for r in conn.execute("SELECT id, name FROM sources").fetchall()
            }
            for r in rows:
                name = r["source_name"] or "(unknown)"
                adj = adjustments.get(id_by_name.get(name, -1), 0)
                print(f"{name:<28} {r['n']:>3} {r['avg_rating']:>6.2f} "
                      f"{r['ups']:>3} {r['downs']:>4}   {adj:+d}")
            return 0

        if not args.article_ids or not (args.up or args.down):
            print("Give one or more article IDs plus --up or --down "
                  "(or use --stats).", file=sys.stderr)
            return 2

        rating = 1 if args.up else -1
        failed = 0
        for article_id in args.article_ids:
            if db.add_feedback(conn, article_id=article_id, rating=rating,
                               note=args.note):
                row = conn.execute(
                    "SELECT title FROM articles WHERE id = ?", (article_id,)
                ).fetchone()
                sign = "+1" if rating > 0 else "-1"
                print(f"{sign} recorded for [{article_id}] {row['title'][:70]}")
            else:
                print(f"Article {article_id} not found.", file=sys.stderr)
                failed += 1
        conn.commit()
        return 1 if failed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())
