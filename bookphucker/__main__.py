import argparse
import sys
import ujson as json
import logging
from shutil import move, rmtree
from getpass import getpass
from pathlib import Path
from contextlib import suppress
from selenium.common.exceptions import (
    WebDriverException,
    TimeoutException,
    NoSuchWindowException,
    InvalidSessionIdException,
)
from bookphucker import Config
from bookphucker.exc import JpDownloadIncomplete, RequiresCapcha
from bookphucker.commonvars import config_path, cache_path
from bookphucker.inputs import parse_cli_inputs
from bookphucker.jp_book_id import jp_cooperation_r, normalize_jp_book_uuid
from bookphucker.user_log import done, headline, step, warn


def _quit_driver(driver) -> None:
    with suppress(WebDriverException, OSError):
        driver.quit()
    # uc.Chrome.__del__ calls quit() again; noop avoids WinError 6 on Windows
    with suppress(Exception):
        driver.quit = lambda *args, **kwargs: None  # type: ignore[method-assign]


def _browser_dead(exc: BaseException) -> bool:
    if isinstance(exc, (NoSuchWindowException, InvalidSessionIdException)):
        return True
    msg = str(exc).lower()
    return (
        "nosuch window" in msg
        or "invalid session id" in msg
        or "disconnected" in msg
        or "web view not found" in msg
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "BookWalker downloader. For payment receipts on Windows, prefer "
            "purchase:SETTLE_UUID instead of a URL containing '&'."
        ),
    )
    parser.add_argument(
        "book_pages",
        help="Book URL or UUID (quote URLs in cmd.exe; or use -f)",
        nargs="*",
        default=[],
    )
    parser.add_argument(
        "-f",
        "--from-file",
        action="append",
        type=Path,
        metavar="PATH",
        help="Text file: one URL/UUID per line (avoids Windows '&' in cmd)",
    )
    parser.add_argument("-r", "--region", help="The region of the bookwalker site",
                        default="auto", choices=["jp", "tw", "auto"])
    parser.add_argument("--no-cache", help="Clear cache directory (cookies, etc.)",
                        action="store_true")
    parser.add_argument("--overwrite", help="Overwrite existing files",
                        action="store_true")
    parser.add_argument(
        "--verify-dir",
        type=Path,
        metavar="PATH",
        help="Audit page_*.png and manifest.json in a babies folder (no download)",
    )

    args = parser.parse_args()
    if args.verify_dir is not None:
        from bookphucker.verify_local import audit_book_folder

        raise SystemExit(audit_book_folder(args.verify_dir.resolve()))
    raw_inputs: list[str] = list(args.book_pages)
    for list_path in args.from_file or []:
        if not list_path.is_file():
            parser.error(f"List file not found: {list_path}")
        for line in list_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw_inputs.append(line)
    if not raw_inputs:
        parser.error("No books given. Pass URLs/UUIDs or use -f books.txt")

    cmd_line = " ".join(sys.argv)
    if "&" in cmd_line and "bookwalker" in cmd_line.lower():
        warn(
            "Command line contains '&' — cmd.exe may have broken your URLs. "
            "Use PowerShell, quote every URL, strip ?query from links, "
            "use bare UUIDs, or put links in a file: -f books.txt"
        )

    parsed = parse_cli_inputs(raw_inputs, region=args.region)
    for note in parsed.warnings:
        warn(note)
    book_uuids = parsed.book_uuids
    purchase_resolve_urls = parsed.purchase_urls
    region = parsed.region
    explicit_book_count = len(book_uuids)
    if region in ("jp", "auto") and book_uuids:
        book_uuids = [normalize_jp_book_uuid(u) for u in book_uuids]
        seen_uuids: set[str] = set()
        deduped: list[str] = []
        for uid in book_uuids:
            if uid in seen_uuids:
                continue
            seen_uuids.add(uid)
            deduped.append(uid)
        if len(deduped) < len(book_uuids):
            step(f"Skipped {len(book_uuids) - len(deduped)} duplicate UUID(s) in queue")
        book_uuids = deduped

    if book_uuids:
        headline(f"Queue: {len(book_uuids)} book(s)")
        for uid in book_uuids:
            step(uid)
    if purchase_resolve_urls:
        step(f"{len(purchase_resolve_urls)} purchase receipt(s) — expand after login")

    match region:
        case "jp" | "auto":
            from bookphucker.jp import (
                login,
                download_book,
                logout,
                resolve_purchase_urls,
            )
        case "tw":
            from bookphucker.tw import login, download_book, logout

    cfg = Config()
    exit_code = 0

    if not config_path.exists():
        user_input = input(
            "Config file not found. Would you like to create one? (Y/n) ").strip().lower() or "y"
        if user_input == "y":
            username = input("Enter your username: ") or None
            password = getpass("Enter your password: ") or None
            cfg = Config(username=username, password=password)
            config_path.write_text(
                json.dumps(cfg.model_dump(mode="json"), indent=2), encoding = "utf-8")
            print(f"Config file created at {config_path}")
    else:
        step(f"Config: {config_path}")
        d = json.loads(config_path.read_text())
        cfg, updated = Config.from_dict(d)
        if updated:
            move(config_path, config_path.with_suffix(".bak"))
            config_path.write_text(
                json.dumps(cfg.model_dump(mode="json"), indent=2), encoding = "utf-8")
            print(f"Config file updated at {config_path}")

    if args.no_cache and cache_path.exists():
        rmtree(cache_path)
        cache_path.mkdir()
        print(f"Cache directory cleared at {cache_path}")

    cfg.config_logging()
    headless = cfg.effective_headless()
    if cfg.headless and cfg.chrome_user_data_dir and not headless:
        step(
            "Chrome: visible window (headless ignored — saved profile needs a real window)"
        )
    elif headless:
        step(
            f"Chrome: headless"
            + (f" (profile {cfg.chrome_user_data_dir})" if cfg.chrome_user_data_dir else "")
        )
    else:
        step(
            "Chrome: visible window"
            + (f" (profile {cfg.chrome_user_data_dir})" if cfg.chrome_user_data_dir else "")
        )
    try:
        driver = cfg.get_webdriver()
    except RuntimeError as chrome_err:
        warn(str(chrome_err))
        return 1

    try:
        username = '' if cfg.manual_login else (
            cfg.username or input("Enter your username: "))
        password = '' if cfg.manual_login else (
            cfg.password or getpass("Enter your password: "))
        max_login_retries = 1
        login_retry = 0
        manual_login = cfg.manual_login or not any([username, password])
        if manual_login and cfg.headless and not cfg.chrome_user_data_dir:
            print("Manual login required, but browser is headless.")
            user_input = input(
                "Would you like to stay headless? (Y/n) ").strip().lower() or "y"
            if user_input != "y":
                driver.quit()
                cfg.headless = False
                driver = cfg.get_webdriver()
        webstore_r = "top%2F"
        if region in ("jp", "auto") and book_uuids:
            webstore_r = jp_cooperation_r(book_uuids[0])
        while True:
            try:
                login(
                    driver,
                    username,
                    password,
                    error_on_captcha=cfg.headless,
                    preserve_browser_session=bool(cfg.chrome_user_data_dir),
                    manual_login_mode=manual_login,
                    webstore_cooperation_r=webstore_r,
                )
            except RequiresCapcha:
                print("Captcha required, but browser is headless.")
                user_input = input(
                    "Would you like to continue with non-headless browser? (y/N) ").strip().lower() or "y"
                if user_input != "y":
                    return 2
                driver.quit()
                cfg.headless = False
                driver = cfg.get_webdriver()
                login(
                    driver,
                    username,
                    password,
                    preserve_browser_session=bool(cfg.chrome_user_data_dir),
                    manual_login_mode=manual_login,
                    webstore_cooperation_r=webstore_r,
                )

            if purchase_resolve_urls:
                if explicit_book_count:
                    step(
                        f"Skipping purchase expansion ({explicit_book_count} book(s) "
                        "already in queue; use only purchase:SETTLE_UUID to auto-detect)"
                    )
                else:
                    for purchase_url in purchase_resolve_urls:
                        for book_uuid in resolve_purchase_urls(driver, purchase_url):
                            if book_uuid not in book_uuids:
                                book_uuids.append(book_uuid)
                                step(f"Added from receipt: {book_uuid}")

            if not book_uuids:
                raise ValueError("No books to download.")

            retry_after_error998 = False
            total_books = len(book_uuids)
            books_ok: list[str] = []
            books_failed: list[tuple[str, str]] = []
            for book_index, book_uuid in enumerate(book_uuids, start=1):
                try:
                    download_book(
                        driver,
                        cfg,
                        book_uuid,
                        overwrite=args.overwrite,
                        book_index=book_index,
                        book_total=total_books,
                    )
                    books_ok.append(book_uuid)
                except TimeoutException as timeout_err:
                    if "ERROR998" in driver.page_source:
                        logging.error("Error 998: Must log out from another device")
                        login_retry += 1
                        if login_retry > max_login_retries:
                            raise
                        logging.warning(
                            "Retrying login %s/%s", login_retry, max_login_retries
                        )
                        logout(driver)
                        retry_after_error998 = True
                        break
                    books_failed.append((book_uuid, str(timeout_err)))
                    warn(
                        f"Timed out on {book_uuid} ({timeout_err}); "
                        "continuing with next book"
                    )
                except JpDownloadIncomplete as incomplete:
                    books_failed.append((book_uuid, str(incomplete)))
                    warn(str(incomplete))
                except Exception as book_error:
                    if _browser_dead(book_error):
                        logging.error(
                            "Chrome window closed or session lost; stopping batch."
                        )
                        warn(
                            "Browser closed or crashed — re-run bookphucker "
                            "(existing page_*.png files are skipped)"
                        )
                        raise
                    books_failed.append((book_uuid, str(book_error)))
                    warn(
                        f"Failed {book_uuid} ({book_error}); continuing with next book"
                    )
            if retry_after_error998:
                continue
            if books_failed:
                exit_code = 1
                warn(
                    f"Batch finished: {len(books_ok)} succeeded, "
                    f"{len(books_failed)} failed"
                )
                for uid, err in books_failed:
                    step(f"FAILED {uid}: {err[:120]}")
            elif books_ok:
                done(f"Batch complete — {len(books_ok)} book(s) OK")
            break
    except Exception as e:
        with suppress(WebDriverException):
            Path("error.html").write_text(driver.page_source, encoding="utf-8")
            Path("error.png").write_bytes(driver.get_screenshot_as_png())
        logging.error(
            "An error occurred. Please check error.html and error.png for more information.")
        raise e
    except KeyboardInterrupt:
        print("Exiting...")
        exit_code = 130
    finally:
        with suppress(NameError):
            _quit_driver(driver)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
