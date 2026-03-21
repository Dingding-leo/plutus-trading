"""
Feedback logger for recording and learning from analysis outcomes.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from .. import config

logger = logging.getLogger(__name__)


def get_feedback_file_path(date: datetime = None) -> Path:
    """
    Get path to feedback log file.

    Args:
        date: Date for the file (default: today)

    Returns:
        Path to file
    """
    if date is None:
        date = datetime.now()

    # Monthly file: YYYY-MM.md
    filename = date.strftime("%Y-%m.md")
    return config.FEEDBACK_DIR / filename


def save_feedback(
    date: datetime,
    my_analysis: str,
    reality: str,
    correction: str,
    lessons: List[str] = None,
) -> Optional[Path]:
    """
    Save feedback to log.

    Args:
        date: Date of analysis
        my_analysis: What I said
        reality: What actually happened
        correction: Correction from Austin
        lessons: List of lessons learned

    Returns:
        Path to saved file, or None on error
    """
    try:
        file_path = get_feedback_file_path(date)

        # Ensure directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Format entry
        date_str = date.strftime("%Y-%m-%d")

        content = f"## {date_str}\n"
        content += f"\n### My Analysis\n"
        content += f"- {my_analysis}\n"
        content += f"\n### Reality\n"
        content += f"- {reality}\n"
        content += f"\n### Correction\n"
        content += f"- {correction}\n"

        if lessons:
            content += f"\n### What I Learned\n"
            for i, lesson in enumerate(lessons, 1):
                content += f"{i}. {lesson}\n"

        content += "\n---\n"

        # Add header if file doesn't exist
        if not file_path.exists():
            month_str = date.strftime("%Y-%m")
            header = f"# Feedback Log - {month_str}\n\n"
            content = header + content

        # Append to file
        with open(file_path, "a") as f:
            f.write(content + "\n")

        return file_path

    except Exception as e:
        logger.error(f"Failed to save feedback: {e}")
        return None


def load_feedback(month: str = None) -> Optional[str]:
    """
    Load feedback log.

    Args:
        month: Month in YYYY-MM format (default: current month)

    Returns:
        Content or None if not found/error
    """
    try:
        if month is None:
            file_path = get_feedback_file_path()
        else:
            # Validate and parse month string
            try:
                year, mon = month.split("-")
                year = int(year)
                mon = int(mon)
                if not (2000 <= year <= 2100 and 1 <= mon <= 12):
                    raise ValueError("Invalid month")
                date = datetime(year, mon, 1)
            except (ValueError, AttributeError) as e:
                logger.error(f"Invalid month format: {month}. Use YYYY-MM format.")
                return None

            file_path = get_feedback_file_path(date)

        if not file_path.exists():
            return None

        with open(file_path, "r") as f:
            return f.read()

    except Exception as e:
        logger.error(f"Failed to load feedback: {e}")
        return None


def ask_feedback_template() -> str:
    """
    Get template for asking feedback.

    Returns:
        Formatted string for prompting user
    """
    return """
Please tell me:
1. Was my analysis correct?
2. What did I miss?
3. What was wrong?
4. What should I focus on next time?
"""
