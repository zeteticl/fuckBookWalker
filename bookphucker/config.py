from __future__ import annotations
import logging
import random
import undetected_chromedriver as uc
from time import sleep
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from semantic_version import Version
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.webdriver.chrome.service import Service


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

    def get_webdriver(self):
        ua = self.user_agent or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        options = uc.ChromeOptions()
        options.set_capability("unhandledPromptBehavior", "accept")
        options.add_argument("--high-dpi-support=1")
        options.add_argument(f"--user-agent={ua}")
        options.add_argument(f"--window-size={self.viewer_size[0]},{self.viewer_size[1]}")
        options.add_argument("--disable-popup-blocking")
        chrome_type = ChromeType.CHROMIUM if self.browser == "chromium" else ChromeType.GOOGLE
        # Install matching ChromeDriver and create a Service
        service = Service(ChromeDriverManager(chrome_type=chrome_type).install())
        # Handle headless mode via options
        if self.headless:
            # use new headless flag for modern Chrome
            options.add_argument("--headless=new")
        elif self.chrome_start_minimized:
            options.add_argument("--start-minimized")
        if self.chrome_user_data_dir:
            options.add_argument(f"--user-data-dir={self.chrome_user_data_dir}")
            if self.chrome_profile_directory:
                options.add_argument(
                    f"--profile-directory={self.chrome_profile_directory}"
                )
        # Initialize undetected_chromedriver with correct service
        driver = uc.Chrome(options=options, service=service)
        return driver

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
        
