from __future__ import annotations
import logging
import random
import re
import subprocess
import sys
from pathlib import Path
import undetected_chromedriver as uc
from time import sleep
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from semantic_version import Version
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException


CURRENT_VERSION = Version("0.2.0")

class Config(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)
    version: str = str(CURRENT_VERSION)
    browser: Literal["chrome", "chromium"] = "chrome"
    headless: bool = True
    username: str | None = None
    password: str | None = None
    manual_login: bool = False
    viewer_size: tuple[int, int] = (1440, 1440)
    user_agent: str | None = None
    logging_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    chrome_user_data_dir: str | None = None
    chrome_profile_directory: str | None = "Default"
    chrome_start_minimized: bool = False
    rate_limit_page_delay_seconds: float = Field(
        default=0.5, ge=0, description="Base delay after each saved page (~reading flip)"
    )
    rate_limit_jitter_seconds: float = Field(
        default=1.0, ge=0, description="Random extra seconds added per page"
    )
    rate_limit_action_delay_seconds: float = Field(default=0.35, ge=0)
    rate_limit_retry_delay_seconds: float = Field(default=0.6, ge=0)
    rate_limit_batch_every_pages: int = Field(
        default=0,
        ge=0,
        description="Every N pages, pause batch_pause_seconds; 0 = off (steady pace only)",
    )
    rate_limit_batch_pause_seconds: float = Field(
        default=5.0, ge=0, description="Long pause when batch_every_pages triggers"
    )
    loading_timeout_seconds: int = Field(default=60, ge=5)
    chrome_launch_retries: int = Field(
        default=3,
        ge=1,
        description="Retries when Chrome fails to start (profile lock, flaky headless)",
    )
    chrome_headless_with_profile: bool = Field(
        default=False,
        description=(
            "If false, headless is ignored when chrome_user_data_dir is set "
            "(recommended on Windows)"
        ),
    )
    jp_force_spread_view: bool = Field(
        default=True,
        description=(
            "JP manga: enable 見開き (face spread) so each file is usually two pages "
            "(better for cross-page art)"
        ),
    )

    def effective_headless(self) -> bool:
        if not self.headless:
            return False
        if self.chrome_user_data_dir and not self.chrome_headless_with_profile:
            return False
        return True

    def rate_limit_after_page(self, page_index: int) -> None:
        if (
            self.rate_limit_batch_every_pages > 0
            and page_index > 0
            and page_index % self.rate_limit_batch_every_pages == 0
        ):
            logging.info(
                "Rate limit: pausing %ss after page %s",
                self.rate_limit_batch_pause_seconds,
                page_index,
            )
            print(
                f"  · Rate limit: pausing {self.rate_limit_batch_pause_seconds:.0f}s "
                f"after page {page_index} (download continues)…",
                flush=True,
            )
            sleep(self.rate_limit_batch_pause_seconds)
        sleep(
            self.rate_limit_page_delay_seconds
            + random.uniform(0, self.rate_limit_jitter_seconds)
        )

    def rate_limit_after_action(self) -> None:
        sleep(self.rate_limit_action_delay_seconds)

    def rate_limit_retry_delay(self) -> None:
        sleep(
            self.rate_limit_retry_delay_seconds
            + random.uniform(0, self.rate_limit_jitter_seconds * 0.5)
        )

    @staticmethod
    def _chrome_profile_busy_hint(
        user_data_dir: str, profile_directory: str | None
    ) -> str | None:
        root = Path(user_data_dir)
        if not root.is_dir():
            return None
        busy_markers = [
            root / "lockfile",
            root / "SingletonLock",
            root / "SingletonCookie",
        ]
        if profile_directory:
            busy_markers.append(root / profile_directory / "LOCK")
        for marker in busy_markers:
            if marker.exists():
                return (
                    f"Chrome profile looks in use ({marker.name}). "
                    "Close all Chrome windows using bookphucker-chrome-profile, "
                    "end stray chrome.exe/chromedriver in Task Manager, "
                    "then retry. Only run one bookphucker at a time."
                )
        return None

    def _build_chrome_options(self) -> uc.ChromeOptions:
        ua = (
            self.user_agent
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        options = uc.ChromeOptions()
        options.set_capability("unhandledPromptBehavior", "accept")
        options.add_argument("--high-dpi-support=1")
        options.add_argument(f"--user-agent={ua}")
        options.add_argument(
            f"--window-size={self.viewer_size[0]},{self.viewer_size[1]}"
        )
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        if self.chrome_start_minimized and not self.effective_headless():
            options.add_argument("--start-minimized")
        if self.chrome_profile_directory:
            options.add_argument(
                f"--profile-directory={self.chrome_profile_directory}"
            )
        return options

    @staticmethod
    def _detect_chrome_version_main() -> int | None:
        if sys.platform == "win32":
            candidates = (
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            )
        else:
            candidates = (
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
            )
        for exe in candidates:
            if not exe.is_file():
                continue
            try:
                out = subprocess.check_output(
                    [str(exe), "--version"],
                    text=True,
                    timeout=15,
                    stderr=subprocess.STDOUT,
                )
                match = re.search(r"(\d+)\.", out)
                if match:
                    return int(match.group(1))
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    def _create_uc_chrome(self, *, headless: bool) -> uc.Chrome:
        options = self._build_chrome_options()
        chrome_type = (
            ChromeType.CHROMIUM if self.browser == "chromium" else ChromeType.GOOGLE
        )
        driver_path = ChromeDriverManager(chrome_type=chrome_type).install()
        version_main = self._detect_chrome_version_main()
        if version_main:
            logging.info("Detected Chrome major version %s", version_main)
        kwargs: dict = {
            "options": options,
            "driver_executable_path": driver_path,
            "headless": headless,
            "user_data_dir": self.chrome_user_data_dir,
            "use_subprocess": True,
        }
        if version_main is not None:
            kwargs["version_main"] = version_main
        return uc.Chrome(**kwargs)

    def get_webdriver(self):
        headless = self.effective_headless()
        if self.headless and self.chrome_user_data_dir and not headless:
            logging.info(
                "headless=true ignored: saved Chrome profile requires a visible window"
            )
        last_err: BaseException | None = None
        for attempt in range(1, self.chrome_launch_retries + 1):
            try:
                return self._create_uc_chrome(headless=headless)
            except (SessionNotCreatedException, WebDriverException, OSError) as exc:
                last_err = exc
                logging.warning(
                    "Chrome launch failed (%s/%s, headless=%s): %s",
                    attempt,
                    self.chrome_launch_retries,
                    headless,
                    exc,
                )
                if attempt < self.chrome_launch_retries:
                    sleep(2 * attempt)
        msg = (
            "Could not start Chrome (session not created / chrome not reachable). "
            "Close other Chrome using the same profile, wait a few seconds, retry. "
        )
        if self.headless and self.chrome_user_data_dir:
            msg += (
                "If it keeps failing, set \"headless\": false in config.json "
                "(headless + saved profile is flaky on Windows). "
            )
        if self.chrome_user_data_dir:
            busy = Config._chrome_profile_busy_hint(
                self.chrome_user_data_dir, self.chrome_profile_directory
            )
            if busy:
                msg += busy
        raise RuntimeError(msg) from last_err

    def config_logging(self):
        level = getattr(logging, self.logging_level)
        logging.basicConfig(level=level)
    
    @classmethod
    def from_dict(cls, data: dict) -> tuple[Config, bool]:
        """
        # Recover Config object from dictionary
        also return if it was updated
        """
        if "version" not in data:
            raise ValueError("Version not found in data")
        data_version = Version(data.pop("version"))
        if (data_version.major, data_version.minor) == (CURRENT_VERSION.major, CURRENT_VERSION.minor):
            return Config(**data), data_version.patch < CURRENT_VERSION.patch
        if data_version.major > CURRENT_VERSION.major:
            raise ValueError("Unsupported config version")
        if data_version < Version("0.2.0"):
            data["viewer_size"] = cls.viewer_size
        return Config(**data), True
        
