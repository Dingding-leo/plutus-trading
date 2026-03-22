"""
feedback command — log trading feedback.
"""

import argparse
from datetime import datetime

from ..utils import feedback_logger


def add_flags(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser("feedback", help="Log feedback")
    p.add_argument("--date", type=str, default=None,
                   help="Date (YYYY-MM-DD, default: today)")
    p.add_argument("--analysis", type=str, required=True,
                   help="What you said")
    p.add_argument("--reality", type=str, required=True,
                   help="What actually happened")
    p.add_argument("--correction", type=str, required=True,
                   help="Correction")
    p.add_argument("--lessons", type=str, default=None,
                   help="Lessons learned (comma-separated)")
    return p


def cmd(args: argparse.Namespace) -> None:
    """Execute the feedback command."""
    print("=" * 60)
    print("PLUTUS - Feedback Logger")
    print("=" * 60)
    print()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    date = datetime.strptime(date_str, "%Y-%m-%d")

    lessons = args.lessons.split(",") if args.lessons else []

    try:
        path = feedback_logger.save_feedback(
            date=date,
            my_analysis=args.analysis,
            reality=args.reality,
            correction=args.correction,
            lessons=lessons,
        )
        print(f"Feedback saved to {path}")
    except Exception as e:
        print(f"Error saving feedback: {e}")
