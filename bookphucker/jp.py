import ujson as json
import bs4
import io
import logging
import re
import time
from urllib.parse import parse_qs, urlparse
from typing import cast
from rich.progress import track
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    JavascriptException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from time import sleep
from pathlib import Path
from bookphucker import Config
from contextlib import suppress
from PIL import Image
from base64 import b64decode
from collections import deque
from .download_manifest import (
    BookManifest,
    all_spreads_verified_on_disk,
    collect_suspect_spreads,
    find_verified_spread_with_hash,
    load_manifest,
    mark_spread_failed,
    mark_spread_verified,
    reconcile_manifest_hashes_from_disk,
    save_manifest,
    sha256_bytes,
    spread_resume_skippable,
    spread_verified_on_disk,
)
from .exc import JpDownloadIncomplete, RequiresCapcha
from .commonvars import cookies_path
from .utils import find_click, save_cookies, recover_cookies, scroll_click
from .user_log import book_header, done, step, warn
from .jp_book_id import (
    JP_PRODUCT_IN_TEXT_RE as _DE_PRODUCT_RE,
    jp_cooperation_r,
    normalize_jp_book_uuid,
    viewer_cid_from_url,
)

domain = "bookwalker.jp"


def _uuids_from_hrefs(hrefs: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        for match in _DE_PRODUCT_RE.finditer(href):
            book_id = match.group(1).lower()
            if book_id not in seen:
                seen.add(book_id)
                found.append(book_id)
    return found


def _uuids_from_driver_links(driver: webdriver.Chrome) -> list[str]:
    hrefs = [
        el.get_attribute("href") or ""
        for el in driver.find_elements(By.CSS_SELECTOR, "a[href]")
    ]
    return _uuids_from_hrefs(hrefs)


_PURCHASE_SCOPE_SELECTORS = (
    "main",
    ".l-main",
    "#main",
    ".m-purchase-detail",
    ".p-purchase-complete",
    ".purchase-complete",
    "[class*='purchase-detail']",
    "[class*='PurchaseDetail']",
    "[class*='purchase-complete']",
)


def _uuids_from_purchase_page(driver: webdriver.Chrome) -> list[str]:
    """Collect product UUIDs from order/receipt content, not global site chrome."""
    soup = bs4.BeautifulSoup(driver.page_source, "lxml")
    scoped_sets: list[list[str]] = []
    for selector in _PURCHASE_SCOPE_SELECTORS:
        for root in soup.select(selector):
            hrefs = [a.get("href", "") for a in root.find_all("a", href=True)]
            uuids = _uuids_from_hrefs(hrefs)
            if uuids:
                scoped_sets.append(uuids)
    if scoped_sets:
        return min(scoped_sets, key=len)
    return _uuids_from_driver_links(driver)


def _settle_uuid_from_purchase_url(purchase_url: str) -> str | None:
    parsed = urlparse(purchase_url)
    settle_uuid = parse_qs(parsed.query).get("settleUuid", [None])[0]
    if settle_uuid:
        return settle_uuid
    m = re.search(r"settleUuid[=:]([0-9a-fA-F]{32})", purchase_url, re.IGNORECASE)
    return m.group(1) if m else None


def resolve_purchase_urls(
    driver: webdriver.Chrome,
    purchase_url: str,
    *,
    max_books: int = 25,
) -> list[str]:
    """Resolve product UUIDs listed on a payment-complete or order URL."""
    logging.info("Resolving purchased book(s) from %s", purchase_url)
    settle_uuid = _settle_uuid_from_purchase_url(purchase_url)

    urls_to_try: list[str] = []
    if settle_uuid:
        urls_to_try.extend(
            (
                f"https://member.{domain}/app/03/my/purchase/detail?settleUuid={settle_uuid}",
                f"https://member.{domain}/app/03/my/settlement/detail?settleUuid={settle_uuid}",
                (
                    "https://bookwalker.jp/member/purchase/complete/"
                    f"?settleUuid={settle_uuid}&platformCode=03"
                ),
            )
        )
    if purchase_url not in urls_to_try:
        urls_to_try.append(purchase_url)

    candidates: list[tuple[int, list[str], str]] = []
    for url in urls_to_try:
        driver.get(url)
        sleep(2)
        uuids = _uuids_from_purchase_page(driver)
        if uuids:
            candidates.append((len(uuids), uuids, url))

    if not candidates:
        raise ValueError(
            "Could not find book link(s) for this purchase. "
            "Pass product URLs or purchase:SETTLE_UUID (32 hex from the receipt)."
        )

    count, uuids, url = min(candidates, key=lambda item: item[0])
    if count > max_books:
        raise ValueError(
            f"Purchase page matched {count} books (likely site navigation noise). "
            "Pass explicit https://bookwalker.jp/de…/ URLs instead of purchase:…"
        )
    logging.info("Resolved %s book(s) from %s", count, url)
    return uuids


def validate_login(driver: webdriver.Chrome) -> bool:
    profile_url = f"https://member.{domain}/app/03/my/profile"
    driver.get(profile_url)
    try:
        WebDriverWait(driver, 15).until(
            lambda d: "/my/profile" in d.current_url
            or "/app/03/login" in d.current_url
        )
    except TimeoutException:
        pass
    current = driver.current_url
    if "/app/03/login" in current:
        return False
    if "ES0001" in driver.page_source:
        return False
    return "/my/profile" in current


def cooperation_url(cooperation_r: str) -> str:
    return (
        f"https://member.{domain}/app/03/webstore/cooperation?r={cooperation_r}"
    )


def webstore_page_ok(driver: webdriver.Chrome) -> bool:
    url = driver.current_url
    if "appleid.apple.com" in url or "/app/03/login" in url:
        return False
    return bool(
        driver.find_elements(By.CLASS_NAME, "t-c-product-main-data__title")
        or driver.find_elements(By.CLASS_NAME, "t-c-read-button")
    )


def dismiss_gdpr_banner(driver: webdriver.Chrome) -> None:
    with suppress(TimeoutException, NoSuchElementException):
        WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.CLASS_NAME, "gdpr-accept"))
        )
        find_click(driver, By.CLASS_NAME, "gdpr-accept")


def webstore_ready(
    driver: webdriver.Chrome,
    cooperation_r: str = "top%2F",
    timeout: int = 15,
) -> bool:
    driver.get(cooperation_url(cooperation_r))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if webstore_page_ok(driver):
            dismiss_gdpr_banner(driver)
            return True
        if "appleid.apple.com" in driver.current_url:
            return False
        sleep(0.4)
    return False


def auto_complete_webstore_login(
    driver: webdriver.Chrome,
    cooperation_r: str,
    timeout: int = 600,
) -> bool:
    member_login_url = f"https://member.{domain}/app/03/login"
    store_url = cooperation_url(cooperation_r)
    driver.get(member_login_url)
    print(
        "Sign in in Chrome only if prompted; the script opens the book page automatically.",
        flush=True,
    )
    deadline = time.time() + timeout
    apple_prompted = False
    while time.time() < deadline:
        if webstore_page_ok(driver):
            dismiss_gdpr_banner(driver)
            print("Webstore session ready.", flush=True)
            return True

        url = driver.current_url
        if "appleid.apple.com" in url:
            if not apple_prompted:
                print("Complete Apple sign-in in Chrome…", flush=True)
                apple_prompted = True
            sleep(2)
            continue

        if validate_login(driver):
            if "cooperation" not in url or cooperation_r not in url:
                print("Opening book on webstore…", flush=True)
                driver.get(store_url)
            dismiss_gdpr_banner(driver)
            sleep(2)
            continue

        driver.get(member_login_url)
        sleep(3)

    return False


def restore_session(
    driver: webdriver.Chrome, cooperation_r: str = "top%2F"
) -> bool:
    logged_in = validate_login(driver)
    if not logged_in and cookies_path.exists():
        if recover_cookies(driver, f"https://{domain}/") and recover_cookies(
            driver, f"https://member.{domain}/app/03/my/profile"
        ):
            logged_in = validate_login(driver)
    if not logged_in:
        return False
    return webstore_ready(driver, cooperation_r)


def login(
    driver: webdriver.Chrome,
    username: str,
    password: str,
    error_on_captcha=False,
    preserve_browser_session=False,
    manual_login_mode=False,
    webstore_cooperation_r: str = "top%2F",
):
    """
    Leave username and password empty for manual login
    """
    member_login_url = f"https://member.{domain}/app/03/login"

    if restore_session(driver, webstore_cooperation_r):
        step("Using saved session (no login required)")
        return

    if not preserve_browser_session:
        driver.delete_all_cookies()

    if manual_login_mode:
        if not auto_complete_webstore_login(driver, webstore_cooperation_r):
            raise TimeoutException(
                "Could not access BookWalker webstore within 10 minutes. "
                "Finish member and Apple sign-in in Chrome, then retry."
            )
    elif username or password:
        driver.get(f"https://member.{domain}/app/03/webstore/cooperation?r=top%2F")
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "mailAddress")))
        sleep(2)
        driver.find_element(By.ID, "mailAddress").send_keys(username)
        sleep(.2)
        driver.find_element(By.ID, "password").send_keys(password)
        sleep(.2)

        try:
            find_click(driver, By.ID, "loginBtn", 1)
        except NoSuchElementException:
            find_click(driver, By.ID, "recaptchaLoginBtn")
            # check if google recaptcha iframe exists
            with suppress(TimeoutException):
                WebDriverWait(driver, 2).until(
                    EC.visibility_of_element_located(
                        (By.CSS_SELECTOR,
                         "iframe[src^='https://www.recaptcha.net/recaptcha/api2/bframe']")))
                if error_on_captcha:
                    raise RequiresCapcha()
        init_time = time.time()
        timeout = 10
        with suppress(NoSuchElementException):
            if driver.find_element(
                By.CSS_SELECTOR,
                "iframe[src^='https://www.recaptcha.net/recaptcha/api2/bframe']"
            ).is_displayed():
                timeout = 180
        while True:
            cookies = driver.get_cookies() or []
            if any(c.get("name") == "bwmember" for c in cookies):
                break
            if time.time() - init_time > timeout:
                raise TimeoutException("Cookies retrieval timeout")
            sleep(.1)

    save_cookies(driver, f"https://{domain}/")
    save_cookies(driver, f"https://member.{domain}/app/03/my/profile")


def logout(driver: webdriver.Chrome):
    driver.get(f"https://member.{domain}/app/03/my/profile")
    find_click(driver, By.CLASS_NAME, "l-header__logout")


def _loading_overlay_visible(driver: webdriver.Chrome) -> bool:
    return driver.execute_script(
        """
        return [...document.getElementsByClassName("loading")].some((el) => {
            const s = getComputedStyle(el);
            if (s.display === "none" || s.visibility === "hidden") {
                return false;
            }
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        });
        """
    )


def wait4loading(driver: webdriver.Chrome, timeout: int = 60) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: not _loading_overlay_visible(d)
        )
        return True
    except TimeoutException:
        logging.warning(
            "Loading overlay did not clear within %ss", timeout
        )
        return False


_NFBR_MENU_KEY_JS = (
    "for (let k in NFBR.a6G.Initializer){"
    "if (NFBR.a6G.Initializer[k]['menu'] !== undefined){ return k; }}"
)


def _ensure_nfbr_context(driver: webdriver.Chrome) -> bool:
    """Switch to the document (top or iframe) where the viewer NFBR API lives."""
    driver.switch_to.default_content()
    with suppress(JavascriptException):
        if driver.execute_script(_NFBR_MENU_KEY_JS):
            return True
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        driver.switch_to.default_content()
        with suppress(WebDriverException):
            driver.switch_to.frame(frame)
            with suppress(JavascriptException):
                if driver.execute_script(_NFBR_MENU_KEY_JS):
                    return True
    driver.switch_to.default_content()
    return False


def get_menu(driver: webdriver.Chrome) -> str:
    if not _ensure_nfbr_context(driver):
        raise JavascriptException("NFBR menu not available")
    obj_name = driver.execute_script(_NFBR_MENU_KEY_JS)
    if not obj_name:
        raise JavascriptException("NFBR menu not available")
    return f"NFBR.a6G.Initializer.{obj_name}.menu"


def get_total_pages(driver: webdriver.Chrome):
    return driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.a2u.r8q.length")


def get_total_spreads(driver: webdriver.Chrome):
    return driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.a2u.r8q.length")

def get_current_page(driver: webdriver.Chrome):
    pages = driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.viewera6e.getPageIndex()")
    return pages+1


def get_current_spread(driver: webdriver.Chrome):
    return 1 + driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.viewera6e.getSpreadIndex()")


def get_spread_page_index(driver: webdriver.Chrome, spread: int) -> int:
    return driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.a2u.r8q[{spread - 1}].pageIndex"
    )


_READ_PAGE_SLIDER_JS = """
const el = document.querySelector("#pageSliderCounter");
if (!el) return null;
const m = (el.textContent || "").trim().match(/^(\\d+)\\s*\\/\\s*(\\d+)$/);
if (!m) return null;
return { current: parseInt(m[1], 10), total: parseInt(m[2], 10) };
"""


def get_page_slider_counter(driver: webdriver.Chrome) -> tuple[int, int] | None:
    _viewer_default_content(driver)
    for _ in _each_browsing_context(driver):
        with suppress(JavascriptException, WebDriverException):
            raw = driver.execute_script(_READ_PAGE_SLIDER_JS)
            if raw and raw.get("current") and raw.get("total"):
                return int(raw["current"]), int(raw["total"])
    _viewer_default_content(driver)
    return None


def expected_book_page_for_spread(driver: webdriver.Chrome, spread: int) -> int:
    return get_spread_page_index(driver, spread) + 1


def viewer_position_matches_spread(driver: webdriver.Chrome, spread: int) -> bool:
    """Confirm spread index and book page (pageSliderCounter when available)."""
    with suppress(JavascriptException):
        _ensure_nfbr_context(driver)
        if get_current_spread(driver) != spread:
            return False
        expected_page = expected_book_page_for_spread(driver, spread)
        slider = get_page_slider_counter(driver)
        if slider is not None:
            current, _total = slider
            if current == expected_page:
                return True
            # 見開き: slider may show either page of the spread.
            if current in (expected_page, expected_page + 1):
                return True
            logging.debug(
                "pageSlider %s/%s for spread %s (expected book page %s)",
                slider[0],
                slider[1],
                spread,
                expected_page,
            )
            return False
        actual_pi = driver.execute_script(
            f"return {get_menu(driver)}.model.attributes.viewera6e.getPageIndex()"
        )
        return actual_pi == get_spread_page_index(driver, spread)
    return False


def viewer_at_spread(driver: webdriver.Chrome, spread: int) -> bool:
    return viewer_position_matches_spread(driver, spread)


def go2page(driver: webdriver.Chrome, page: int):
    driver.execute_script(f"{get_menu(driver)}.options.a6l.moveToPage({page-1});")


def go2spread(driver: webdriver.Chrome, spread: int):
    page_index = driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.a2u.r8q[{spread-1}].pageIndex")
    go2page(driver, page_index+1)


_FORCE_SPREAD_VIEW_JS = """
const menu = __MENU__;
function collectSpreadSetters(root, depth, out) {
  if (!root || depth > 4) return;
  let names;
  try {
    names = Object.getOwnPropertyNames(root);
  } catch (e) {
    return;
  }
  for (const name of names) {
    if (!/spread|見開|pageview|pagelayout|facing/i.test(name)) continue;
    let value;
    try {
      value = root[name];
    } catch (e) {
      continue;
    }
    if (typeof value === "function") {
      for (const arg of [1, 2, true]) {
        out.push(() => value.call(root, arg));
      }
    } else if (typeof value === "number") {
      out.push(() => {
        root[name] = 1;
      });
      out.push(() => {
        root[name] = 2;
      });
    } else if (typeof value === "boolean") {
      out.push(() => {
        root[name] = true;
      });
    }
  }
  for (const name of names) {
    try {
      const child = root[name];
      if (child && typeof child === "object") {
        collectSpreadSetters(child, depth + 1, out);
      }
    } catch (e) {}
  }
}
const targets = [];
const roots = [
  menu.options,
  menu.options && menu.options.a6l,
  menu.model && menu.model.attributes,
  menu.model && menu.model.attributes && menu.model.attributes.viewera6e,
  menu.model && menu.model.attributes && menu.model.attributes.a2u,
];
for (const root of roots) collectSpreadSetters(root, 0, targets);
for (const run of targets) {
  try {
    run();
    return true;
  } catch (e) {}
}
return false;
"""


def force_spread_view(driver: webdriver.Chrome) -> bool:
    """Try to switch BookWalker reader to 見開き (face spread) display."""
    if not _ensure_nfbr_context(driver):
        return False
    menu = get_menu(driver)
    script = _FORCE_SPREAD_VIEW_JS.replace("__MENU__", menu)
    with suppress(JavascriptException, WebDriverException):
        return bool(driver.execute_script(script))
    return False


_VIEWER_CID_URLS = (
    "https://viewer.bookwalker.jp/03/30/viewer.html?cid={cid}&cty=1",
    "https://viewer.bookwalker.jp/v/ng/viewer?cid={cid}",
    "https://viewer.bookwalker.jp/v/ng/browser_viewer?cid={cid}",
)
_READ_BUTTON_SELECTORS = (
    "a.t-c-read-button",
    "button.t-c-read-button",
    ".t-c-read-button a",
    "a[class*='t-c-read-button']",
    ".t-c-read-button",
)
_DEBUG_DIR = Path("debug")


def _viewer_debug(msg: str, *args) -> None:
    logging.debug("[viewer] " + msg, *args)


def _save_viewer_debug_artifacts(
    driver: webdriver.Chrome, book_uuid: str, attempt: int, label: str
) -> None:
    _DEBUG_DIR.mkdir(exist_ok=True)
    stem = _DEBUG_DIR / f"viewer_{book_uuid}_{attempt}_{label}"
    with suppress(WebDriverException):
        stem.with_suffix(".png").write_bytes(driver.get_screenshot_as_png())
    with suppress(WebDriverException):
        stem.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
    _viewer_debug("Saved %s.png / %s.html", stem.name, stem.name)


def _log_viewer_browser_state(
    driver: webdriver.Chrome, book_uuid: str, label: str
) -> None:
    _viewer_debug("--- %s (book %s) ---", label, book_uuid)
    with suppress(WebDriverException):
        _viewer_debug("current_url=%s", driver.current_url)
        _viewer_debug("title=%s", driver.title)
    handles = driver.window_handles
    _viewer_debug("window_handles=%s", len(handles))
    for idx, handle in enumerate(handles):
        with suppress(WebDriverException):
            driver.switch_to.window(handle)
            has_canvas = bool(driver.find_elements(By.CSS_SELECTOR, ".currentScreen canvas"))
            cid = viewer_cid_from_url(driver.current_url or "")
            _viewer_debug(
                "  handle[%s] url=%s canvas=%s cid=%s",
                idx,
                driver.current_url,
                has_canvas,
                cid,
            )
    driver.switch_to.default_content()
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    _viewer_debug("top-level iframes=%s", len(iframes))
    for idx, frame in enumerate(iframes[:8]):
        src = (frame.get_attribute("src") or "")[:160]
        _viewer_debug("  iframe[%s] src=%s", idx, src)
    for ctx_name in _each_browsing_context(driver):
        for sel in _READ_BUTTON_SELECTORS:
            matches = driver.find_elements(By.CSS_SELECTOR, sel)
            if not matches:
                continue
            _viewer_debug(
                "context=%s selector=%s count=%s",
                ctx_name,
                sel,
                len(matches),
            )
            for i, el in enumerate(matches[:3]):
                _viewer_debug(
                    "  match[%s] tag=%s displayed=%s enabled=%s text=%r href=%r onclick=%r",
                    i,
                    el.tag_name,
                    el.is_displayed(),
                    el.is_enabled(),
                    (el.text or "")[:60],
                    (el.get_attribute("href") or "")[:120],
                    (el.get_attribute("onclick") or "")[:80],
                )
    driver.switch_to.default_content()
    with suppress(WebDriverException):
        yomu = driver.find_elements(
            By.XPATH, "//*[contains(normalize-space(.), '読む')]"
        )
        _viewer_debug("elements containing '読む' (default content)=%s", len(yomu))


def _each_browsing_context(driver: webdriver.Chrome):
    """Switch driver into each top-level frame context; yield context label."""
    driver.switch_to.default_content()
    yield "default"
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for idx, frame in enumerate(frames):
        driver.switch_to.default_content()
        with suppress(WebDriverException):
            driver.switch_to.frame(frame)
            yield f"iframe[{idx}]"
    driver.switch_to.default_content()


def _read_href_matches_book(href: str | None, book_uuid: str) -> bool:
    if not href:
        return False
    uid = book_uuid.lower()
    upper = href.upper()
    return uid in href.lower() or f"BROWSER_VIEWER/{uid.upper()}" in upper


def _find_read_button(driver: webdriver.Chrome, book_uuid: str, timeout: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        fallback: tuple[str, str, object] | None = None
        for ctx_name in _each_browsing_context(driver):
            for sel in _READ_BUTTON_SELECTORS:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if not el.is_displayed():
                        continue
                    href = _read_button_href(el) or (el.get_attribute("href") or "")
                    if _read_href_matches_book(href, book_uuid):
                        _viewer_debug(
                            "Using read control context=%s selector=%s (uuid match)",
                            ctx_name,
                            sel,
                        )
                        return el
                    if fallback is None and href:
                        fallback = (ctx_name, sel, el)
        if fallback is not None:
            ctx_name, sel, el = fallback
            logging.warning(
                "No 読む link contained uuid %s; using first href in %s",
                book_uuid,
                ctx_name,
            )
            _viewer_debug(
                "Using read control context=%s selector=%s (fallback)",
                ctx_name,
                sel,
            )
            return el
        sleep(0.4)
    driver.switch_to.default_content()
    raise TimeoutException("読む control not found in page or iframes")


_VIEWER_CANVAS_SELECTOR = ".currentScreen canvas"

# Composite every visible spread canvas (BookWalker uses 2 canvases for 2-page spreads).
_CAPTURE_SPREAD_JS = """
function visibleCanvases(root) {
  return Array.from(root.querySelectorAll("canvas")).filter((c) => {
    if (c.width < 2 || c.height < 2) return false;
    const r = c.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(c);
    return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) > 0.01;
  });
}

function collectCanvases() {
  const current = document.querySelector(".currentScreen");
  if (!current) return [];
  return visibleCanvases(current);
}

function dedupeStackedCanvases(canvases) {
  const out = [];
  for (const c of canvases) {
    const r = c.getBoundingClientRect();
    const idx = out.findIndex(
      (d) => Math.abs(d.getBoundingClientRect().left - r.left) < 3
        && Math.abs(d.getBoundingClientRect().top - r.top) < 3
    );
    if (idx >= 0) out[idx] = c;
    else out.push(c);
  }
  return out;
}

function captureSpread() {
  let canvases = dedupeStackedCanvases(collectCanvases());
  if (!canvases.length) return null;
  canvases.sort(
    (a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left
  );
  if (canvases.length === 1) {
    return canvases[0].toDataURL("image/png");
  }
  const out = document.createElement("canvas");
  out.width = canvases.reduce((sum, c) => sum + c.width, 0);
  out.height = Math.max(...canvases.map((c) => c.height));
  const ctx = out.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, out.width, out.height);
  let x = 0;
  for (const c of canvases) {
    ctx.drawImage(c, x, 0);
    x += c.width;
  }
  return out.toDataURL("image/png");
}

return captureSpread();
"""

_CANVAS_DIGEST_JS = """
function visibleCanvases(root) {
  return Array.from(root.querySelectorAll("canvas")).filter((c) => {
    if (c.width < 2 || c.height < 2) return false;
    const r = c.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(c);
    return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) > 0.01;
  });
}

function digestSpreadCanvases() {
  const current = document.querySelector(".currentScreen");
  if (!current) return null;
  const canvases = visibleCanvases(current);
  if (!canvases.length) return null;
  canvases.sort(
    (a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left
  );
  let acc = String(canvases.length);
  for (const c of canvases) {
    const ctx = c.getContext("2d");
    const w = c.width;
    const h = c.height;
    const sw = Math.min(48, w);
    const sh = Math.min(48, h);
    const data = ctx.getImageData(0, 0, sw, sh).data;
    let s = 0;
    for (let i = 0; i < data.length; i += 16) {
      s = (s + data[i] + data[i + 1] + data[i + 2]) | 0;
    }
    acc += "|" + w + "x" + h + ":" + s;
  }
  return acc;
}

return digestSpreadCanvases();
"""


def _data_url_to_png_bytes(data_url: str) -> bytes:
    payload = data_url.split(",", 1)[-1]
    return b64decode(payload)


def _spread_canvas_digest(driver: webdriver.Chrome) -> str | None:
    _viewer_default_content(driver)
    for _ in _each_browsing_context(driver):
        with suppress(JavascriptException, WebDriverException):
            digest = driver.execute_script(_CANVAS_DIGEST_JS)
            if digest:
                return str(digest)
    _viewer_default_content(driver)
    return None


def _image_is_all_black(img: Image.Image) -> bool:
    rgb = img.convert("RGB")
    w, h = rgb.size
    if w == 0 or h == 0:
        return True
    step_x = max(1, w // 24)
    step_y = max(1, h // 24)
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            if any(rgb.getpixel((x, y))):
                return False
    return True


def _spread_image_looks_half_blank(img: Image.Image) -> bool:
    w, h = img.size
    if w < int(h * 1.15):
        return False
    left = img.crop((0, 0, w // 2, h)).convert("L")
    pixels = left.getdata()
    if not pixels:
        return False
    near_white = sum(1 for p in pixels if p > 248)
    return near_white / len(pixels) > 0.9


def _capture_spread_png_bytes(driver: webdriver.Chrome) -> bytes | None:
    _viewer_default_content(driver)
    for _ in _each_browsing_context(driver):
        with suppress(JavascriptException, WebDriverException):
            data_url = driver.execute_script(_CAPTURE_SPREAD_JS)
            if data_url:
                return _data_url_to_png_bytes(data_url)
    _viewer_default_content(driver)
    return None


def _viewer_default_content(driver: webdriver.Chrome) -> None:
    driver.switch_to.default_content()


def _find_viewer_canvas(driver: webdriver.Chrome):
    _viewer_default_content(driver)
    canvases = driver.find_elements(By.CSS_SELECTOR, _VIEWER_CANVAS_SELECTOR)
    if canvases:
        return canvases[0]
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        _viewer_default_content(driver)
        with suppress(WebDriverException):
            driver.switch_to.frame(frame)
            canvases = driver.find_elements(By.CSS_SELECTOR, _VIEWER_CANVAS_SELECTOR)
            if canvases:
                return canvases[0]
    _viewer_default_content(driver)
    return None


def _handle_has_viewer_canvas(driver: webdriver.Chrome) -> bool:
    return _find_viewer_canvas(driver) is not None


def _viewer_url_matches_book(driver: webdriver.Chrome, book_uuid: str) -> bool:
    url = driver.current_url or ""
    cid = viewer_cid_from_url(url)
    if cid:
        return cid == book_uuid.lower()
    return book_uuid.lower() in url.lower()


def _viewer_matches_book(driver: webdriver.Chrome, book_uuid: str) -> bool:
    if not _viewer_url_matches_book(driver, book_uuid):
        return False
    return _find_viewer_canvas(driver) is not None


def _viewer_page_ready(driver: webdriver.Chrome, book_uuid: str) -> bool:
    if not _viewer_url_matches_book(driver, book_uuid):
        return False
    if _find_viewer_canvas(driver) is not None:
        return True
    with suppress(TimeoutException):
        for _ in _each_browsing_context(driver):
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, _VIEWER_CANVAS_SELECTOR)
                )
            )
            if _find_viewer_canvas(driver) is not None:
                return True
    return _viewer_matches_book(driver, book_uuid)


def _close_extra_windows(driver: webdriver.Chrome, keep: str) -> None:
    for handle in list(driver.window_handles):
        if handle == keep:
            continue
        with suppress(WebDriverException):
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(keep)


def _wait_for_viewer(
    driver: webdriver.Chrome,
    product_handle: str,
    timeout: float,
    book_uuid: str,
) -> None:
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        for handle in driver.window_handles:
            with suppress(WebDriverException):
                driver.switch_to.window(handle)
                if _viewer_page_ready(driver, book_uuid):
                    _viewer_debug("Viewer ready on handle url=%s", driver.current_url)
                    return
        now = time.time()
        if now - last_log >= 8.0:
            _viewer_debug(
                "Still waiting for viewer (%.0fs left) handles=%s",
                deadline - now,
                len(driver.window_handles),
            )
            _log_viewer_browser_state(driver, book_uuid, "wait-for-viewer")
            last_log = now
        sleep(0.4)
    raise TimeoutException("Book viewer did not open after 読む")


def _read_button_href(element) -> str | None:
    href = (element.get_attribute("href") or "").strip()
    if href and not href.lower().startswith("javascript:"):
        return href
    if element.tag_name.lower() != "a":
        with suppress(NoSuchElementException):
            parent = element.find_element(By.XPATH, "./ancestor::a[1]")
            href = (parent.get_attribute("href") or "").strip()
            if href and not href.lower().startswith("javascript:"):
                return href
    return None


def _launch_viewer_via_read_href(driver: webdriver.Chrome, book_uuid: str) -> bool:
    element = _find_read_button(driver, book_uuid)
    href = _read_button_href(element)
    if not href:
        _viewer_debug("Read control has no usable href (tag=%s)", element.tag_name)
        return False
    if href.startswith("/"):
        href = f"https://{domain}{href}"
    _viewer_debug("Navigating to 読む href: %s", href)
    driver.get(href)
    return True


def _launch_viewer_via_read_click(driver: webdriver.Chrome, book_uuid: str) -> None:
    handles_before = set(driver.window_handles)
    element = _find_read_button(driver, book_uuid)
    _viewer_debug(
        "Clicking read control tag=%s href=%r",
        element.tag_name,
        element.get_attribute("href"),
    )
    scroll_click(driver, element)
    sleep(1.5)
    handles_after = set(driver.window_handles)
    new_handles = handles_after - handles_before
    _viewer_debug(
        "After click: handles %s -> %s (new=%s)",
        len(handles_before),
        len(handles_after),
        len(new_handles),
    )


def _launch_viewer_direct(driver: webdriver.Chrome, book_uuid: str) -> None:
    for template in _VIEWER_CID_URLS:
        url = template.format(cid=book_uuid)
        _viewer_debug("Trying direct viewer URL: %s", url)
        driver.get(url)
        sleep(2)
        if _viewer_matches_book(driver, book_uuid):
            return
    _viewer_debug("Direct viewer URLs did not show canvas yet")


def open_jp_viewer(driver: webdriver.Chrome, cfg: Config, book_uuid: str) -> None:
    """Open the JP viewer via 読む link, click, or direct cid URL."""
    book_uuid = normalize_jp_book_uuid(book_uuid)
    product_handle = driver.current_window_handle
    max_attempts = 5
    viewer_timeout = max(35.0, min(cfg.loading_timeout_seconds, 90))

    for attempt in range(1, max_attempts + 1):
        dismiss_gdpr_banner(driver)
        _log_viewer_browser_state(driver, book_uuid, f"before attempt {attempt}")
        strategy = "href+click"
        try:
            if attempt >= 3:
                strategy = "direct"
                _launch_viewer_direct(driver, book_uuid)
            elif attempt == 2:
                strategy = "click"
                _launch_viewer_via_read_click(driver, book_uuid)
            else:
                strategy = "href+click"
                if not _launch_viewer_via_read_href(driver, book_uuid):
                    _launch_viewer_via_read_click(driver, book_uuid)
            _log_viewer_browser_state(driver, book_uuid, f"after {strategy}")
            _wait_for_viewer(driver, product_handle, viewer_timeout, book_uuid)
            step(f"Reader opened ({strategy}, attempt {attempt})")
            return
        except (TimeoutException, WebDriverException) as exc:
            warn(f"Reader open failed ({strategy}, attempt {attempt}/{max_attempts}): {exc}")
            _log_viewer_browser_state(driver, book_uuid, f"failed attempt {attempt}")
            _save_viewer_debug_artifacts(
                driver, book_uuid, attempt, strategy.replace("+", "_")
            )
            _close_extra_windows(driver, product_handle)
            if attempt < max_attempts:
                webstore_ready(driver, jp_cooperation_r(book_uuid), timeout=20)
            cfg.rate_limit_after_action()
            sleep(1)

    raise TimeoutException(f"Could not open viewer after {max_attempts} attempts")


def wait_for_spread_ready(
    driver: webdriver.Chrome, spread: int, timeout: float
) -> bool:
    _ensure_nfbr_context(driver)
    deadline = time.time() + timeout
    stable_hits = 0
    while time.time() < deadline:
        with suppress(JavascriptException):
            if get_current_spread(driver) == spread and viewer_at_spread(
                driver, spread
            ):
                stable_hits += 1
                if stable_hits >= 2:
                    return True
            else:
                stable_hits = 0
        sleep(0.15)
    return False


def wait_for_canvas_digest_stable(
    driver: webdriver.Chrome, timeout: float
) -> bool:
    deadline = time.time() + timeout
    last: str | None = None
    stable_hits = 0
    while time.time() < deadline:
        digest = _spread_canvas_digest(driver)
        if digest is None:
            stable_hits = 0
            sleep(0.12)
            continue
        if digest == last:
            stable_hits += 1
            if stable_hits >= 2:
                return True
        else:
            last = digest
            stable_hits = 1
        sleep(0.12)
    return last is not None


def wait_for_spread_canvas_ready(
    driver: webdriver.Chrome,
    spread: int,
    previous_digest: str | None,
    nav_from_spread: int | None,
    timeout: float,
) -> bool:
    """Wait until spread/page position is correct and canvas is stable."""
    deadline = time.time() + timeout
    relax_digest_at = time.time() + min(10.0, timeout * 0.45)
    need_new_digest = (
        nav_from_spread is not None
        and nav_from_spread != spread
        and previous_digest is not None
    )
    while time.time() < deadline:
        if not viewer_position_matches_spread(driver, spread):
            sleep(0.15)
            continue
        digest = _spread_canvas_digest(driver)
        if digest is None:
            sleep(0.15)
            continue
        if (
            need_new_digest
            and time.time() < relax_digest_at
            and digest == previous_digest
        ):
            sleep(0.2)
            continue
        sleep(0.22)
        again = _spread_canvas_digest(driver)
        if again == digest and viewer_position_matches_spread(driver, spread):
            return True
        sleep(0.1)
    return False


def _try_current_spread(driver: webdriver.Chrome) -> int | None:
    with suppress(JavascriptException):
        _ensure_nfbr_context(driver)
        return get_current_spread(driver)
    return None


def _product_uuid_from_page(driver: webdriver.Chrome) -> str | None:
    url = driver.current_url or ""
    m = re.search(
        r"/de([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        url,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    soup = bs4.BeautifulSoup(driver.page_source, "lxml")
    og = soup.find("meta", attrs={"property": "og:url"})
    content = og.get("content") if og else None
    if content:
        with suppress(ValueError):
            return normalize_jp_book_uuid(content)
    return None


def _assert_product_page(driver: webdriver.Chrome, book_uuid: str) -> None:
    found = _product_uuid_from_page(driver)
    if found and found != book_uuid:
        raise ValueError(
            f"Product page is book {found}, expected {book_uuid} "
            "(browser may still be on a previous title)."
        )


def _reset_tabs_for_next_book(driver: webdriver.Chrome, book_uuid: str) -> None:
    while len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])
        driver.close()
    driver.switch_to.window(driver.window_handles[0])
    url = driver.current_url or ""
    if _handle_has_viewer_canvas(driver) or "viewer." in url:
        webstore_ready(driver, jp_cooperation_r(book_uuid), timeout=30)


def _seed_prev_imgs_for_spread(
    save_dir: Path,
    manifest: BookManifest,
    spread: int,
    prev_imgs: deque[bytes],
) -> None:
    prev_imgs.clear()
    for s in range(spread - 1, max(0, spread - 3), -1):
        rec = manifest.spreads.get(s)
        path = save_dir / f"page_{s}.png"
        if not path.is_file():
            continue
        data = path.read_bytes()
        if rec and rec.sha256 and sha256_bytes(data) != rec.sha256:
            continue
        prev_imgs.append(data)


def _previous_spread_png_bytes(
    save_dir: Path, manifest: BookManifest, spread: int
) -> bytes | None:
    if spread <= 1:
        return None
    prev = spread - 1
    path = save_dir / f"page_{prev}.png"
    if not path.is_file():
        return None
    data = path.read_bytes()
    rec = manifest.spreads.get(prev)
    if rec and rec.sha256 and sha256_bytes(data) != rec.sha256:
        return None
    return data


def _sync_book_manifest(
    save_dir: Path,
    book_uuid: str,
    total_spreads: int,
    jp_force_spread_view: bool,
    overwrite: bool,
) -> BookManifest:
    existing = load_manifest(save_dir)
    if existing is None:
        return BookManifest(
            book_uuid=book_uuid,
            total_spreads=total_spreads,
            jp_force_spread_view=jp_force_spread_view,
        )
    if (
        existing.book_uuid != book_uuid
        or existing.total_spreads != total_spreads
        or existing.jp_force_spread_view != jp_force_spread_view
    ):
        warn(
            "Manifest mismatch (book id, spread count, or 見開き) — "
            "spread verification records were reset"
        )
        return BookManifest(
            book_uuid=book_uuid,
            total_spreads=total_spreads,
            jp_force_spread_view=jp_force_spread_view,
        )
    if overwrite:
        return BookManifest(
            book_uuid=book_uuid,
            total_spreads=total_spreads,
            jp_force_spread_view=jp_force_spread_view,
        )
    existing.total_spreads = total_spreads
    existing.jp_force_spread_view = jp_force_spread_view
    return existing


def _finish_download_or_raise(
    title: str,
    save_dir: Path,
    manifest: BookManifest,
    total_spreads: int,
) -> None:
    reconcile_manifest_hashes_from_disk(manifest, save_dir)
    save_manifest(save_dir, manifest)
    verified = sum(
        1
        for s in range(1, total_spreads + 1)
        if spread_verified_on_disk(manifest, s, save_dir)
    )
    suspects = collect_suspect_spreads(manifest)
    failed = [
        s
        for s in range(1, total_spreads + 1)
        if not spread_verified_on_disk(manifest, s, save_dir)
    ]
    if verified == total_spreads and not suspects:
        done(f"Finished — {verified}/{total_spreads} spreads verified in babies/{title}")
        return
    if failed:
        sample = failed[:12]
        extra = f" (+{len(failed) - len(sample)} more)" if len(failed) > len(sample) else ""
        warn(f"Incomplete spreads: {sample}{extra}")
    if suspects:
        warn(f"Duplicate-hash suspects: {suspects[:8]}")
    raise JpDownloadIncomplete(
        title=title,
        verified=verified,
        total_spreads=total_spreads,
        failed_spreads=failed,
        suspect_pairs=suspects,
    )


def _leave_viewer_after_download(driver: webdriver.Chrome, book_uuid: str) -> None:
    with suppress(WebDriverException):
        if len(driver.window_handles) > 1:
            driver.close()
            driver.switch_to.window(driver.window_handles[0])
        elif _handle_has_viewer_canvas(driver) or "viewer." in (driver.current_url or ""):
            webstore_ready(driver, jp_cooperation_r(book_uuid), timeout=20)


def download_book(
    driver: webdriver.Chrome,
    cfg: Config,
    book_uuid: str,
    overwrite,
    *,
    book_index: int = 1,
    book_total: int = 1,
):
    book_uuid = normalize_jp_book_uuid(book_uuid)
    _reset_tabs_for_next_book(driver, book_uuid)
    step("Loading product page…")
    if not webstore_ready(driver, jp_cooperation_r(book_uuid), timeout=30):
        raise ValueError(
            "BookWalker webstore session is not available. "
            "Sign in again (including Apple ID if prompted) and retry."
        )
    _assert_product_page(driver, book_uuid)

    soup = bs4.BeautifulSoup(driver.page_source, "lxml")

    title_blk = soup.find(class_="t-c-product-main-data__title")
    if title_blk is None:
        raise ValueError("Title not found")
    title = title_blk.text.strip()
    authors_blk = soup.find(class_="t-c-product-main-data__authors")
    authors = [a.text.strip() for a in cast(bs4.Tag, authors_blk).find_all("dd")] if authors_blk else []

    book_header(book_index, book_total, title, book_uuid)
    if authors:
        step(f"Author(s): {', '.join(authors)}")

    save_dir = Path(f"babies/{title}")
    save_dir.mkdir(exist_ok=True, parents=True)
    meta_path = save_dir / "meta.json"

    pre_manifest = load_manifest(save_dir)
    if pre_manifest and pre_manifest.book_uuid == book_uuid and not overwrite:
        if reconcile_manifest_hashes_from_disk(pre_manifest, save_dir):
            save_manifest(save_dir, pre_manifest)
    if (
        pre_manifest
        and pre_manifest.book_uuid == book_uuid
        and not overwrite
        and pre_manifest.total_spreads > 0
        and all_spreads_verified_on_disk(
            pre_manifest, pre_manifest.total_spreads, save_dir
        )
    ):
        step(
            f"All {pre_manifest.total_spreads} spreads verified in manifest — "
            "skipping reader"
        )
        done(
            f"Finished — {pre_manifest.total_spreads}/{pre_manifest.total_spreads} "
            f"spreads verified in babies/{title}"
        )
        return

    driver.set_window_size(*cfg.viewer_size)

    step("Opening reader…")
    open_jp_viewer(driver, cfg, book_uuid)
    if not _viewer_matches_book(driver, book_uuid):
        raise ValueError(
            f"Viewer did not open for {book_uuid} (url={driver.current_url})"
        )

    _viewer_default_content(driver)
    wait4loading(driver, timeout=cfg.loading_timeout_seconds)

    if cfg.jp_force_spread_view:
        if force_spread_view(driver):
            step("Reader: 見開き (two-page spread) enabled")
        else:
            warn(
                "Could not enable 見開き automatically — in the reader menu set "
                "Spread Display (見開き) to face spread, then re-download"
            )
        wait4loading(driver, timeout=cfg.loading_timeout_seconds)
        sleep(1)

    spreads_deadline = time.time() + max(120, cfg.loading_timeout_seconds * 2)
    while time.time() < spreads_deadline:
        try:
            total_spreads = get_total_spreads(driver)
            break
        except JavascriptException:
            _ensure_nfbr_context(driver)
            sleep(0.5)
    else:
        raise TimeoutException("Total spreads retrieval timeout")
    step(f"Spreads: {total_spreads} (capture unit = spread, not single book page)")
    meta_path.write_text(
        json.dumps(
            {
                "title": title,
                "authors": authors,
                "book_uuid": book_uuid,
                "total_spreads": total_spreads,
                "jp_force_spread_view": cfg.jp_force_spread_view,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = _sync_book_manifest(
        save_dir, book_uuid, total_spreads, cfg.jp_force_spread_view, overwrite
    )
    healed = reconcile_manifest_hashes_from_disk(manifest, save_dir)
    if healed:
        save_manifest(save_dir, manifest)
        step(f"Reconciled {healed} spread hash(es) with on-disk PNGs")
    verified_before = sum(
        1
        for s in range(1, total_spreads + 1)
        if spread_resume_skippable(manifest, s, save_dir)
    )
    if verified_before and not overwrite:
        step(
            f"Saving {total_spreads} spreads to babies/{title} "
            f"(resume: {verified_before} verified in manifest)"
        )
    else:
        step(f"Saving {total_spreads} spreads to babies/{title}")

    if not overwrite and all_spreads_verified_on_disk(
        manifest, total_spreads, save_dir
    ):
        step(f"All {total_spreads} spreads verified — skipping capture")
        _leave_viewer_after_download(driver, book_uuid)
        done(
            f"Finished — {total_spreads}/{total_spreads} spreads verified "
            f"in babies/{title}"
        )
        return

    spreads_to_capture = [
        s
        for s in range(1, total_spreads + 1)
        if overwrite or not spread_resume_skippable(manifest, s, save_dir)
    ]
    if spreads_to_capture:
        first = spreads_to_capture[0]
        if not viewer_position_matches_spread(driver, first):
            go2spread(driver, first)
            sleep(1)
        else:
            sleep(0.3)

    prev_imgs = deque[bytes](maxlen=2)
    manifest_writes_since_flush = 0

    def _persist_manifest(force: bool = False) -> None:
        nonlocal manifest_writes_since_flush
        if force:
            save_manifest(save_dir, manifest)
            manifest_writes_since_flush = 0
            return
        manifest_writes_since_flush += 1
        if manifest_writes_since_flush >= 5:
            save_manifest(save_dir, manifest)
            manifest_writes_since_flush = 0
    max_retries = 12
    bar_label = f"Spreads · {title[:40]}"
    for current_spread in track(
        spreads_to_capture, description=bar_label, total=len(spreads_to_capture)
    ):
        retry = 0
        savename = save_dir / f"page_{current_spread}.png"
        nav_timeout = min(90.0, max(45.0, cfg.loading_timeout_seconds * 1.5))
        page_index: int | None = None
        try:
            _ensure_nfbr_context(driver)
            _seed_prev_imgs_for_spread(save_dir, manifest, current_spread, prev_imgs)
            nav_from_spread = _try_current_spread(driver)
            before_digest = _spread_canvas_digest(driver)
            page_index = get_spread_page_index(driver, current_spread)
            if not viewer_position_matches_spread(driver, current_spread):
                go2spread(driver, current_spread)
            if not wait_for_spread_ready(driver, current_spread, nav_timeout):
                logging.warning(
                    "Spread %s navigation slow; retrying once", current_spread
                )
                go2spread(driver, current_spread)
                if not wait_for_spread_ready(driver, current_spread, nav_timeout):
                    mark_spread_failed(
                        manifest,
                        current_spread,
                        "navigation timeout",
                        page_index=page_index,
                    )
                    _persist_manifest(force=True)
                    logging.error(
                        "Spread %s navigation failed; not saved", current_spread
                    )
                    continue
            cfg.rate_limit_after_action()
            if not wait4loading(driver, timeout=cfg.loading_timeout_seconds):
                mark_spread_failed(
                    manifest,
                    current_spread,
                    "loading overlay timeout",
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s loading did not finish; not saved", current_spread
                )
                continue
            if not viewer_position_matches_spread(driver, current_spread):
                slider = get_page_slider_counter(driver)
                mark_spread_failed(
                    manifest,
                    current_spread,
                    "spread/page slider mismatch after navigation"
                    + (f" (slider={slider})" if slider else ""),
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s position not confirmed (slider=%s); not saved",
                    current_spread,
                    slider,
                )
                continue
            canvas_timeout = min(35.0, max(18.0, float(cfg.loading_timeout_seconds) * 0.6))
            canvas_ok = wait_for_spread_canvas_ready(
                driver,
                current_spread,
                before_digest,
                nav_from_spread,
                canvas_timeout,
            )
            if not canvas_ok:
                mark_spread_failed(
                    manifest,
                    current_spread,
                    "canvas not ready after navigation",
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s canvas not ready after navigation; not saved",
                    current_spread,
                )
                continue

            img = None
            img_bytes: bytes | None = None
            accepted = False
            while retry < max_retries:
                if not viewer_position_matches_spread(driver, current_spread):
                    logging.debug(
                        "Spread %s position mismatch before capture; re-navigating",
                        current_spread,
                    )
                    go2spread(driver, current_spread)
                    sleep(0.4)
                    retry += 1
                    cfg.rate_limit_retry_delay()
                    continue
                raw = _capture_spread_png_bytes(driver)
                if raw is None:
                    logging.debug(
                        "No spread capture for spread %s, retrying", current_spread
                    )
                    retry += 1
                    cfg.rate_limit_retry_delay()
                    continue
                img_bytes = raw
                img = Image.open(io.BytesIO(img_bytes))
                if _image_is_all_black(img):
                    logging.debug(
                        "Blank spread %s, treated as unloaded", current_spread
                    )
                elif _spread_image_looks_half_blank(img):
                    logging.debug(
                        "Spread %s looks half-blank (missing left page), retrying",
                        current_spread,
                    )
                elif img_bytes in prev_imgs:
                    logging.debug(
                        "Spread %s frame unchanged (buffer duplicate), re-navigating",
                        current_spread,
                    )
                    go2spread(driver, current_spread)
                    sleep(0.5)
                    wait_for_spread_canvas_ready(
                        driver,
                        current_spread,
                        None,
                        nav_from_spread,
                        min(15.0, canvas_timeout),
                    )
                else:
                    digest = sha256_bytes(img_bytes)
                    other = find_verified_spread_with_hash(
                        manifest, current_spread, digest
                    )
                    if other is not None:
                        logging.debug(
                            "Spread %s matches verified spread %s; retrying",
                            current_spread,
                            other,
                        )
                    else:
                        prev_bytes = _previous_spread_png_bytes(
                            save_dir, manifest, current_spread
                        )
                        if prev_bytes is not None and img_bytes == prev_bytes:
                            logging.debug(
                                "Spread %s matches previous spread file; retrying",
                                current_spread,
                            )
                        else:
                            prev_imgs.append(img_bytes)
                            accepted = True
                            break
                retry += 1
                logging.debug(
                    "Retrying spread %s (%s/%s)", current_spread, retry, max_retries
                )
                cfg.rate_limit_retry_delay()

            if not accepted or img is None or img_bytes is None:
                mark_spread_failed(
                    manifest,
                    current_spread,
                    "capture validation exhausted",
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s not verified; not saved", current_spread
                )
                continue

            if not viewer_position_matches_spread(driver, current_spread):
                mark_spread_failed(
                    manifest,
                    current_spread,
                    "spread index changed after capture",
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s moved after capture; not saved", current_spread
                )
                continue

            digest = sha256_bytes(img_bytes)
            other = find_verified_spread_with_hash(
                manifest, current_spread, digest
            )
            if other is not None:
                mark_spread_failed(
                    manifest,
                    current_spread,
                    f"duplicate hash of spread {other}",
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s duplicate of spread %s; not saved",
                    current_spread,
                    other,
                )
                continue

            savename.write_bytes(img_bytes)
            dup = mark_spread_verified(
                manifest,
                current_spread,
                img_bytes,
                img.width,
                img.height,
                page_index if page_index is not None else 0,
            )
            if dup is not None:
                with suppress(OSError):
                    savename.unlink(missing_ok=True)
                mark_spread_failed(
                    manifest,
                    current_spread,
                    f"duplicate hash of spread {dup}",
                    page_index=page_index,
                )
                _persist_manifest(force=True)
                logging.error(
                    "Spread %s duplicate of spread %s; removed file",
                    current_spread,
                    dup,
                )
                continue
            _persist_manifest()
            logging.debug("Saved verified spread %s", current_spread)
            _viewer_default_content(driver)
        except Exception as page_error:
            mark_spread_failed(
                manifest,
                current_spread,
                f"exception: {page_error}",
                page_index=page_index,
            )
            _persist_manifest(force=True)
            logging.error(
                "Failed spread %s (%s); not saved",
                current_spread,
                page_error,
            )
            continue
        cfg.rate_limit_after_page(current_spread)

    _persist_manifest(force=True)
    _leave_viewer_after_download(driver, book_uuid)
    _finish_download_or_raise(title, save_dir, manifest, total_spreads)
