import ujson as json
import bs4
import io
import logging
import time
from typing import cast
from rich.progress import track
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.common.exceptions import JavascriptException
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
from .utils import find_click, save_cookies, recover_cookies

domain = "bookwalker.jp"


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
        print("Using saved BookWalker session (no login required).", flush=True)
        logging.info("Using saved BookWalker session")
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


def get_menu(driver: webdriver.Chrome) -> str:
    obj_name = driver.execute_script(
        "for (let k in NFBR.a6G.Initializer){"
        "if (NFBR.a6G.Initializer[k]['menu'] !== undefined){ return k; }}")
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


def wait_for_spread(
    driver: webdriver.Chrome, spread: int, timeout: float
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with suppress(JavascriptException):
            if get_current_spread(driver) == spread:
                return True
        sleep(0.15)
    return False


def download_book(driver: webdriver.Chrome, cfg: Config, book_uuid: str, overwrite):
    logging.info("Downloading book %s", book_uuid)
    cooperation_r = f"de{book_uuid}%2F"
    if not webstore_ready(driver, cooperation_r, timeout=30):
        raise ValueError(
            "BookWalker webstore session is not available. "
            "Sign in again (including Apple ID if prompted) and retry."
        )

    soup = bs4.BeautifulSoup(driver.page_source, "lxml")

    title_blk = soup.find(class_="t-c-product-main-data__title")
    if title_blk is None:
        raise ValueError("Title not found")
    title = title_blk.text.strip()
    authors_blk = soup.find(class_="t-c-product-main-data__authors")
    authors = [a.text.strip() for a in cast(bs4.Tag, authors_blk).find_all("dd")] if authors_blk else []

    logging.info("Titled %s by %s", title, ", ".join(authors))

    driver.set_window_size(*cfg.viewer_size)

    dismiss_gdpr_banner(driver)

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CLASS_NAME, "t-c-read-button")))

    find_click(driver, By.CLASS_NAME, "t-c-read-button")

    save_dir = Path(f"babies/{title}")
    save_dir.mkdir(exist_ok=True, parents=True)
    meta_path = save_dir / "meta.json"
    meta_path.write_text(json.dumps(
        {"title": title, "authors": authors},
        ensure_ascii=False, indent=2), encoding="utf-8")

    driver.switch_to.window(driver.window_handles[-1])

    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".currentScreen canvas")))

    WebDriverWait(driver, 30).until(
        EC.invisibility_of_element_located((By.CLASS_NAME, "progressbar")))

    init_time = time.time()
    while True:
        try:
            total_spreads = get_total_spreads(driver)
            break
        except JavascriptException:
            if time.time() - init_time > 10:
                raise TimeoutException("Total spreads retrieval timeout")
    prev_imgs = deque[bytes](maxlen=2)  # used for checking update for 2 buffers
    max_retries = 30
    logging.info("Total spreads: %s", total_spreads)

    go2spread(driver, 1)
    sleep(2)

    for current_spread in track(range(1, total_spreads + 1), description="Downloading", total=total_spreads):
        retry = 0
        savename = save_dir / f"page_{current_spread}.png"
        if savename.exists() and not overwrite:
            logging.debug("page %s already exists, skipping", current_spread)
            continue
        nav_timeout = max(120.0, cfg.loading_timeout_seconds * 2)
        try:
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
            canvas = driver.find_element(By.CSS_SELECTOR, ".currentScreen canvas")
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
        except Exception as page_error:
            logging.error(
                "Failed spread %s (%s); continuing download",
                current_spread,
                page_error,
            )
            continue
        cfg.rate_limit_after_page(current_spread)
