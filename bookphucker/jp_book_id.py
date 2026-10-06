"""BookWalker JP product identifiers (UUID) — one canonical form for all inputs."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

BOOK_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_JP_PRODUCT_PATH_RE = re.compile(
    r"^de([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
JP_PRODUCT_IN_TEXT_RE = re.compile(
    r"de([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

JP_DOMAIN = "bookwalker.jp"


def normalize_jp_book_uuid(ref: str) -> str:
    """
    Accept a bare UUID, de-prefixed slug, or bookwalker.jp / viewer URL.
    Always returns lowercase canonical UUID (no de prefix).
    """
    raw = ref.strip().strip("/")
    if not raw:
        raise ValueError("Empty book reference")

    if BOOK_UUID_RE.fullmatch(raw):
        return raw.lower()

    if raw.lower().startswith(("purchase:", "settle:")):
        raise ValueError("Purchase receipt is not a book UUID")

    url = raw
    if not url.startswith("http"):
        if "bookwalker" in url.lower():
            url = "https://" + url.lstrip("/")
        elif _JP_PRODUCT_PATH_RE.match(raw):
            return _JP_PRODUCT_PATH_RE.match(raw).group(1).lower()  # type: ignore[union-attr]
        else:
            m = JP_PRODUCT_IN_TEXT_RE.search(raw)
            if m:
                return m.group(1).lower()
            raise ValueError(f"Not a BookWalker JP book UUID or URL: {ref}")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "bookwalker" not in host:
        raise ValueError(f"Not a BookWalker URL: {ref}")

    if host.startswith("viewer."):
        cid = parse_qs(parsed.query).get("cid", [None])[0]
        if not cid or not BOOK_UUID_RE.fullmatch(cid):
            raise ValueError(f"Could not read cid from viewer URL: {ref}")
        return cid.lower()

    slug = (parsed.path or "").strip("/")
    path_match = _JP_PRODUCT_PATH_RE.match(slug)
    if path_match:
        return path_match.group(1).lower()

    m = JP_PRODUCT_IN_TEXT_RE.search(url)
    if m:
        return m.group(1).lower()

    raise ValueError(f"Could not parse book UUID from: {ref}")


def viewer_cid_from_url(url: str) -> str | None:
    cid = parse_qs(urlparse(url).query).get("cid", [None])[0]
    if cid and BOOK_UUID_RE.fullmatch(cid):
        return cid.lower()
    return None


def jp_product_slug(book_uuid: str) -> str:
    return f"de{normalize_jp_book_uuid(book_uuid)}"


def jp_product_url(book_uuid: str) -> str:
    return f"https://{JP_DOMAIN}/{jp_product_slug(book_uuid)}/"


def jp_cooperation_r(book_uuid: str) -> str:
    return f"{jp_product_slug(book_uuid)}%2F"
