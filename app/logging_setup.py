"""Central logging configuration.

Configured once, in main.py, before anything else runs. Every other module
just does `logger = logging.getLogger(__name__)` and starts logging -- it
never configures anything itself.
"""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Send timestamped logs to the console.

    Args:
        level: One of DEBUG, INFO, WARNING, ERROR. Unknown values fall
               back to INFO rather than crashing the program.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,  # replace any handler a library installed first
    )
