from __future__ import annotations

"""Shared helpers for obtaining platform tokens via Playwright."""

import asyncio
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional, Sequence, Tuple

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright


class PlaywrightTokenFetcher(ABC):
    """Base class that automates login and captures authorization headers."""

    network_idle_timeout_ms: int = 180_000

    @property
    @abstractmethod
    def login_url(self) -> str:
        """Initial URL used to start the login flow."""

    @property
    def login_urls(self) -> Sequence[str]:
        """Optional list of login URLs to try, in order of preference."""
        return [self.login_url]

    @property
    @abstractmethod
    def target_endpoints(self) -> Sequence[str]:
        """Endpoints whose requests should carry the authorization header."""

    @abstractmethod
    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        """Types the provided username and password into the page."""

    @abstractmethod
    async def submit_login(self, page: Page) -> None:
        """Triggers the login form submission."""

    async def dismiss_cookie_banner(self, page: Page) -> None:  # pragma: no cover - UI dependent
        """Best-effort cookie dismissal. Platforms may override for custom behavior."""
        return None

    def fetch_token(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        wait_for_user_confirmation: Optional[Callable[[], None]] = None,
    ) -> str:
        """
        Synchronously obtains the bearer token after authenticating with credentials.

        When a running event loop is detected (e.g., inside a UI app), the coroutine is
        executed in a background thread to avoid nested-loop errors.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.fetch_token_async(
                    username,
                    password,
                    headless=headless,
                    wait_for_user_confirmation=wait_for_user_confirmation,
                )
            )

        return self._fetch_token_in_thread(
            username,
            password,
            headless=headless,
            wait_for_user_confirmation=wait_for_user_confirmation,
        )

    async def fetch_token_async(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        wait_for_user_confirmation: Optional[Callable[[], None]] = None,
    ) -> str:
        manual_login = not (username and password)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            page = await browser.new_page()

            try:
                navigation_error: BaseException | None = None

                for candidate in self.login_urls:
                    try:
                        await page.goto(candidate, wait_until="domcontentloaded")
                        await page.wait_for_load_state(
                            "networkidle", timeout=self.network_idle_timeout_ms
                        )
                        break
                    except BaseException as exc:
                        navigation_error = exc
                else:
                    raise navigation_error or RuntimeError("Falha ao abrir a página de login.")

                await self.dismiss_cookie_banner(page)
                auth_task = asyncio.create_task(self._capture_authorization_header(page))

                if not manual_login:
                    await self.fill_credentials(page, username, password)
                    await self.submit_login(page)

                try:
                    await page.wait_for_load_state("networkidle", timeout=self.network_idle_timeout_ms)
                except PlaywrightTimeoutError:
                    # Continue even if the page remains busy; the request listener might still capture the token.
                    pass

                auth_header, _ = await auth_task

                if not auth_header:
                    raise ValueError("Não foi possível capturar o token de autorização durante o login.")
                return self._strip_bearer_prefix(auth_header)
            finally:
                if wait_for_user_confirmation:
                    try:
                        await asyncio.to_thread(wait_for_user_confirmation)
                    except Exception:
                        pass

                await browser.close()

    def _fetch_token_in_thread(
        self,
        username: str,
        password: str,
        *,
        headless: bool,
        wait_for_user_confirmation: Optional[Callable[[], None]],
    ) -> str:
        result: list[str] = []
        exc: list[BaseException] = []
        finished = threading.Event()

        def runner() -> None:
            try:
                result.append(
                    asyncio.run(
                        self.fetch_token_async(
                            username,
                            password,
                            headless=headless,
                            wait_for_user_confirmation=wait_for_user_confirmation,
                        )
                    )
                )
            except BaseException as error:  # pragma: no cover - pass-through error handling
                exc.append(error)
            finally:
                finished.set()

        threading.Thread(target=runner, daemon=True).start()
        finished.wait()

        if exc:
            raise exc[0]

        if not result:
            raise RuntimeError("Falha interna ao capturar o token via Playwright.")

        return result[0]

    async def _capture_authorization_header(self, page: Page) -> Tuple[Optional[str], Optional[str]]:
        def matches_target(url: str) -> bool:
            return any(url.startswith(endpoint) for endpoint in self.target_endpoints)

        try:
            request = await page.wait_for_event(
                "request",
                predicate=lambda r: matches_target(r.url),
                timeout=self.network_idle_timeout_ms,
            )
            return request.headers.get("authorization"), request.url
        except PlaywrightTimeoutError:
            return None, None

    def _strip_bearer_prefix(self, header: str) -> str:
        prefix = "bearer "
        if header.lower().startswith(prefix):
            return header[len(prefix):].strip()
        return header.strip()
