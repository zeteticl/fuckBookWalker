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
from .exc import RequiresCapcha
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


def wait4loading(driver: webdriver.Chrome, timeout: int = 60):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: not _loading_overlay_visible(d)
        )
    except TimeoutException:
        logging.warning(
            "Loading overlay did not clear within %ss; continuing anyway", timeout
        )


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


def go2page(driver: webdriver.Chrome, page: int):
    driver.execute_script(f"{get_menu(driver)}.options.a6l.moveToPage({page-1});")


def go2spread(driver: webdriver.Chrome, spread: int):
    page_index = driver.execute_script(
        f"return {get_menu(driver)}.model.attributes.a2u.r8q[{spread-1}].pageIndex")
    go2page(driver, page_index+1)


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


def wait_for_spread(
    driver: webdriver.Chrome, spread: int, timeout: float
) -> bool:
    _ensure_nfbr_context(driver)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with suppress(JavascriptException):
            if get_current_spread(driver) == spread:
                return True
        sleep(0.15)
    return False


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

    driver.set_window_size(*cfg.viewer_size)

    step("Opening reader…")
    open_jp_viewer(driver, cfg, book_uuid)
    if not _viewer_matches_book(driver, book_uuid):
        raise ValueError(
            f"Viewer did not open for {book_uuid} (url={driver.current_url})"
        )

    save_dir = Path(f"babies/{title}")
    save_dir.mkdir(exist_ok=True, parents=True)
    meta_path = save_dir / "meta.json"
    meta_path.write_text(
        json.dumps(
            {"title": title, "authors": authors, "book_uuid": book_uuid},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    _viewer_default_content(driver)
    wait4loading(driver, timeout=cfg.loading_timeout_seconds)

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
    step(f"Spreads: {total_spreads}")
    prev_imgs = deque[bytes](maxlen=2)  # used for checking update for 2 buffers
    max_retries = 30
    existing_pages = sum(1 for p in save_dir.glob("page_*.png"))
    if existing_pages and not overwrite:
        step(
            f"Saving {total_spreads} pages to babies/{title} "
            f"(resume: {existing_pages} already on disk)"
        )
    else:
        step(f"Saving {total_spreads} pages to babies/{title}")

    if (
        not overwrite
        and existing_pages >= total_spreads
        and (save_dir / f"page_{total_spreads}.png").exists()
    ):
        step(f"All {total_spreads} pages on disk — skipping capture")
        _leave_viewer_after_download(driver, book_uuid)
        done(f"Finished — {total_spreads}/{total_spreads} pages in babies/{title}")
        return

    go2spread(driver, 1)
    sleep(2)

    bar_label = f"Pages · {title[:40]}"
    for current_spread in track(
        range(1, total_spreads + 1), description=bar_label, total=total_spreads
    ):
        retry = 0
        savename = save_dir / f"page_{current_spread}.png"
        if savename.exists() and not overwrite:
            logging.debug("page %s already exists, skipping", current_spread)
            continue
        nav_timeout = max(120.0, cfg.loading_timeout_seconds * 2)
        try:
            _ensure_nfbr_context(driver)
            go2spread(driver, current_spread)
            if not wait_for_spread(driver, current_spread, nav_timeout):
                logging.warning(
                    "Spread %s navigation slow; retrying once", current_spread
                )
                go2spread(driver, current_spread)
                if not wait_for_spread(driver, current_spread, nav_timeout):
                    logging.error(
                        "Skipping spread %s after navigation timeout; continuing book",
                        current_spread,
                    )
                    continue
            cfg.rate_limit_after_action()
            wait4loading(driver, timeout=cfg.loading_timeout_seconds)
            logging.debug("Getting page %s out of %s", current_spread, total_spreads)
            canvas = _find_viewer_canvas(driver)
            if canvas is None:
                raise TimeoutException(
                    f"Viewer canvas not found for spread {current_spread}"
                )
            img = None
            while retry < max_retries:
                canvas_base64 = driver.execute_script(
                    "return arguments[0].toDataURL('image/png').slice(21);", canvas)
                img_bytes = b64decode(canvas_base64)
                img = Image.open(io.BytesIO(img_bytes))
                if all(all(v == 0 for v in c) for c in img.getdata()):
                    logging.debug(
                        "Blank page %s, treated as unloaded page", current_spread
                    )
                elif img_bytes not in prev_imgs:
                    prev_imgs.append(img_bytes)
                    break
                retry += 1
                logging.debug(
                    "Retrying page %s (%s/%s)", current_spread, retry, max_retries
                )
                cfg.rate_limit_retry_delay()
            if retry == max_retries:
                logging.warning("Potentially repeated page %s", current_spread)
            if img is None:
                logging.error(
                    "No image for spread %s; skipping page", current_spread
                )
                continue
            img.save(savename)
            logging.debug("Saved page %s", current_spread)
            _viewer_default_content(driver)
        except Exception as page_error:
            logging.error(
                "Failed spread %s (%s); continuing download",
                current_spread,
                page_error,
            )
            continue
        cfg.rate_limit_after_page(current_spread)

    _leave_viewer_after_download(driver, book_uuid)
    saved = sum(1 for p in save_dir.glob("page_*.png"))
    done(f"Finished — {saved}/{total_spreads} pages in babies/{title}")
