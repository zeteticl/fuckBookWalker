"""English, user-facing progress lines (INFO also goes to the logging module)."""

from __future__ import annotations

import logging


def step(message: str) -> None:
    logging.info(message)
    print(f"  • {message}", flush=True)


def headline(message: str) -> None:
    logging.info(message)
    print(f"\n{message}", flush=True)


def book_header(index: int, total: int, title: str, book_uuid: str) -> None:
    headline(f"Book {index}/{total}: {title}")
    step(f"UUID {book_uuid}")


def warn(message: str) -> None:
    logging.warning(message)
    print(f"  ! {message}", flush=True)


def done(message: str) -> None:
    logging.info(message)
    print(f"  OK {message}", flush=True)
