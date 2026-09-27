"""Opt-in rotating diagnostic log without leaking URL query tokens."""

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


LOGGER = logging.getLogger("yummy_anime_manager")
LOGGER.setLevel(logging.CRITICAL + 1)
LOGGER.propagate = False
LOG_DIR = Path.home() / ".yummy_anime_manager" / "logs"
LOG_FILE = LOG_DIR / "application.log"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
TOKEN_RE = re.compile(r"(X-Application\s*[:=]\s*)\S+", re.I)


def safe_url(url):
    parts = urlsplit(str(url or ""))
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        message = URL_RE.sub(lambda match: safe_url(match.group()), message)
        return TOKEN_RE.sub(r"\1<redacted>", message)


def configure_logging(enabled):
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(logging.DEBUG if enabled else logging.CRITICAL + 1)
    LOGGER.propagate = False
    if enabled:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(RedactingFormatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.info("Diagnostic logging enabled; Python %s", sys.version.split()[0])


def install_exception_hooks():
    def handle_unhandled(exc_type, exc, traceback):
        LOGGER.critical("Unhandled application exception", exc_info=(exc_type, exc, traceback))
        sys.__excepthook__(exc_type, exc, traceback)

    def handle_thread(args):
        LOGGER.critical("Unhandled thread exception: %s", args.thread.name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = handle_unhandled
    threading.excepthook = handle_thread
