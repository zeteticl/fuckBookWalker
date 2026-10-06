"""Parse CLI / pasted BookWalker URLs into book UUIDs and purchase receipts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlparse

import logging
import requests

from bookphucker.jp_book_id import BOOK_UUID_RE, normalize_jp_book_uuid

Region = Literal["jp", "tw", "auto"]

_SETTLE_UUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SETTLE_IN_TEXT_RE = re.compile(
    r"settleUuid[=:]([0-9a-fA-F]{32})",
    re.IGNORECASE,
)
_BOOKWALKER_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+bookwalker[^\s\"'<>]*",
    re.IGNORECASE,
)


def purchase_complete_url(settle_uuid: str) -> str:
    return (
        "https://bookwalker.jp/member/purchase/complete/"
        f"?settleUuid={settle_uuid}&platformCode=03"
    )


def _split_tokens(raw_argv: list[str]) -> list[str]:
    tokens: list[str] = []
    for part in raw_argv:
        for chunk in re.split(r"[\s\r\n]+", part.strip()):
            chunk = chunk.strip().strip(",").strip(";")
            if chunk:
                tokens.append(chunk)
    return tokens


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


@dataclass
class ParsedCliInputs:
    region: Region = "auto"
    book_uuids: list[str] = field(default_factory=list)
    purchase_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _note_settle(
    inputs: ParsedCliInputs,
    settle_uuid: str,
    *,
    source: str,
) -> None:
    settle_uuid = settle_uuid.lower()
    url = purchase_complete_url(settle_uuid)
    _append_unique(inputs.purchase_urls, url)
    logging.debug("Purchase receipt queued (%s): %s", source, url)


def _add_jp_book(inputs: ParsedCliInputs, ref: str) -> None:
    book_uuid = normalize_jp_book_uuid(ref)
    if inputs.region == "auto":
        inputs.region = "jp"
    _append_unique(inputs.book_uuids, book_uuid)
    logging.debug("Queued book UUID %s", book_uuid)


def _parse_tw_url(inputs: ParsedCliInputs, url: str) -> None:
    if inputs.region == "auto":
        inputs.region = "tw"
    parsed = urlparse(url)
    book_id = ""
    if parsed.path.startswith("/product/"):
        book_id = parsed.path.removeprefix("/product/").split("/")[0]
        r = requests.head(
            f"https://www.bookwalker.com.tw/browserViewer/{book_id}/trial",
            timeout=30,
        )
        parsed = urlparse(r.headers["Location"])
    query = parsed.query
    book_uuid = dict([param.split("=") for param in query.split("&")])["cid"]
    _append_unique(inputs.book_uuids, book_uuid)
    logging.debug("Queued TW book UUID %s", book_uuid)


def _classify_token(inputs: ParsedCliInputs, token: str) -> None:
    stripped = token.strip()
    lower = stripped.lower()

    if lower.startswith(("purchase:", "settle:")):
        settle_uuid = stripped.split(":", 1)[1].strip()
        if not _SETTLE_UUID_RE.fullmatch(settle_uuid):
            raise ValueError(
                f"Invalid settle UUID '{settle_uuid}'. Expected 32 hex characters."
            )
        _note_settle(inputs, settle_uuid, source=f"from {lower.split(':')[0]}:")
        return

    if _SETTLE_UUID_RE.fullmatch(stripped):
        _note_settle(inputs, stripped, source="from 32-char settle id")
        return

    path = urlparse(stripped).path if stripped.startswith("http") else ""
    if "purchase/complete" in path or "/my/purchase/detail" in path:
        settle = parse_qs(urlparse(stripped).query).get("settleUuid", [None])[0]
        if settle:
            _note_settle(inputs, settle, source="from purchase URL")
        else:
            _append_unique(inputs.purchase_urls, stripped)
            logging.debug("Purchase receipt URL queued: %s", stripped)
        return

    if ".com.tw" in lower and "bookwalker" in lower:
        if not stripped.startswith("http"):
            stripped = "https://" + stripped
        _parse_tw_url(inputs, stripped)
        return

    if BOOK_UUID_RE.fullmatch(stripped) or "bookwalker" in lower or stripped.startswith("de"):
        _add_jp_book(inputs, stripped)
        return

    raise ValueError(
        f"Unrecognized book argument '{stripped}'. "
        "Use a book UUID or https://bookwalker.jp/de…/ URL."
    )


def parse_cli_inputs(
    raw_argv: list[str],
    *,
    region: Region = "auto",
) -> ParsedCliInputs:
    inputs = ParsedCliInputs(region=region)
    tokens = _split_tokens(raw_argv)
    blob = " ".join(raw_argv)

    if "&" in blob and "settleUuid" in blob:
        inputs.warnings.append(
            "Detected '&' with settleUuid — also scanning joined arguments for "
            "purchase receipts (use purchase:SETTLE_UUID to avoid .cmd issues)."
        )

    seen_tokens: set[str] = set()
    for settle in _SETTLE_IN_TEXT_RE.findall(blob):
        _note_settle(inputs, settle, source="from settleUuid in arguments")

    for url in _BOOKWALKER_URL_RE.findall(blob):
        if url not in seen_tokens:
            seen_tokens.add(url)
            _classify_token(inputs, url)

    for token in tokens:
        if token in seen_tokens:
            continue
        if token in ("platformCode=03",) or token.startswith("platformCode="):
            continue
        seen_tokens.add(token)
        _classify_token(inputs, token)

    for w in inputs.warnings:
        logging.warning("%s", w)

    return inputs
