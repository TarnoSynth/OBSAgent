"""``McpRuntime`` - lifecycle lokalnego serwera MCP w event loopie agenta (Faza 1).

**Kontrakt:**

- ``await runtime.start()`` - startuje serwer w ``asyncio.Task`` i czeka
  na ready health check (``uvicorn.Server.started`` albo HTTP GET na
  ``/mcp`` z odpowiedzią 200/405/406 - co dowodzi, że socket jest pod
  adresem i gotowy). Przy kolizji portów rzuca ``OSError`` zamieniony na
  ``RuntimeError`` z czytelnym komunikatem.
- ``await runtime.stop()`` - graceful shutdown: ``server.should_exit=True``,
  czeka do ``shutdown_timeout`` na naturalne zamknięcie, w razie czego
  robi ``Task.cancel()`` jako fallback.
- ``async with runtime:`` - context manager wrapper nad ``start()/stop()``.
  Preferowany flow w ``main.py``.

**Idempotencja:** powtórne ``start()`` po już uruchomionym serwerze jest
no-op. ``stop()`` na nieuruchomionym runtime też jest no-op. Dzięki temu
``Agent.ensure_mcp_started()`` jest łatwy do wołania z różnych miejsc.

**Warunki wykrycia kolizji portu:**

Zamiast czekać aż uvicorn padnie w tle (co jest nieczytelne w logach
biegu agenta), robimy **pre-flight bind test** na ``host:port``. Jeśli
socket się nie zbinduje - od razu rzucamy błąd z instrukcją "zmień
``mcp.port`` w config.yaml". Race condition jest teoretyczny (ktoś inny
zajmie port między naszym testem a uvicorn bind) i akceptowalny dla
lokalnego dev.

**Czas życia:**

Zgodnie z planem fazy 1: ``McpRuntime`` żyje **cały bieg agenta** (wszystkie
commity z kolejki), a ``ToolExecutionContext`` jest świeży per commit.
Serwer nie restartuje się między commitami - ``ctx_provider`` dostarcza
świeży context na żądanie.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

import httpx
import uvicorn

from src.agent.mcp.config import McpSettings

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from logs.run_logger import RunLogger

logger = logging.getLogger(__name__)


_SHUTDOWN_TIMEOUT_S = 3.0


class McpRuntime:
    """Wrapper odpalający ``FastMCP`` (streamable-http) w asyncio.Task.

    Typowe użycie z poziomu ``main.py``::

        runtime = McpRuntime(mcp=mcp, settings=settings, run_logger=rl)
        async with runtime:
            # agent biega tutaj, MCP serwer żyje w tle
            ...
        # serwer zatrzymany, task sprzątnięty
    """

    def __init__(
        self,
        *,
        mcp: "FastMCP",
        settings: McpSettings,
        run_logger: "RunLogger | None" = None,
    ) -> None:
        self._mcp = mcp
        self._settings = settings
        self._run_logger = run_logger

        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def started(self) -> bool:
        """``True`` gdy serwer jest aktualnie uruchomiony i przyjmuje ruch."""

        return self._started

    @property
    def url(self) -> str:
        """URL streamable-http endpointu (delegate do ``McpSettings.url``)."""

        return self._settings.url

    async def start(self) -> None:
        """Uruchamia serwer i czeka na ready. Idempotentne.

        Rzuca ``RuntimeError`` przy kolizji portu albo timeoucie startu.
        Nie łapie innych wyjątków - propaguje do wołacza.
        """

        if self._started:
            return

        self._preflight_port_check()

        starlette_app = self._mcp.streamable_http_app()
        config = uvicorn.Config(
            starlette_app,
            host=self._settings.host,
            port=self._settings.port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(),
            name=f"mcp-server-{self._settings.server_name}",
        )

        try:
            await self._wait_until_ready()
        except Exception:
            await self._force_stop()
            raise

        self._started = True
        if self._run_logger is not None:
            self._run_logger.log_mcp_server_started(
                host=self._settings.host,
                port=self._settings.port,
                url=self._settings.url,
                server_name=self._settings.server_name,
                transport=self._settings.transport,
            )
        logger.info(
            "MCP server wstartował: %s (transport=%s)",
            self._settings.url,
            self._settings.transport,
        )

    async def stop(self) -> None:
        """Zatrzymuje serwer. Idempotentne.

        Najpierw graceful: ``server.should_exit=True``. Jeśli task nie
        skończy się w ``_SHUTDOWN_TIMEOUT_S`` - cancel jako fallback.
        """

        if not self._started and self._task is None:
            return

        await self._force_stop()

        if self._run_logger is not None:
            self._run_logger.log_mcp_server_stopped(
                host=self._settings.host,
                port=self._settings.port,
            )
        logger.info("MCP server zatrzymany: %s", self._settings.url)

    async def __aenter__(self) -> "McpRuntime":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------

    def _preflight_port_check(self) -> None:
        """Próbuje zbindować gniazdko na ``host:port`` i od razu zamknąć.

        Dzięki temu kolizja portu jest widoczna **zanim** uvicorn zacznie
        logować swoje błędy - i user dostaje czytelny komunikat.
        """

        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                test_sock.bind((self._settings.host, self._settings.port))
            except OSError as exc:
                raise RuntimeError(
                    f"Port {self._settings.host}:{self._settings.port} jest zajęty "
                    f"({exc}). Zmień `mcp.port` w config.yaml albo zatrzymaj proces, "
                    f"który trzyma ten port."
                ) from exc
        finally:
            test_sock.close()

    async def _wait_until_ready(self) -> None:
        """Poll ``server.started`` + HTTP ping na ``/mcp`` do limitu czasu.

        ``uvicorn.Server.started`` staje się True zaraz po ``startup``
        event (socket jest nasłuchujący). HTTP ping potwierdza, że
        aplikacja Starlette odpowiada.
        """

        deadline = asyncio.get_event_loop().time() + self._settings.startup_timeout_s
        sleep_s = 0.05

        while True:
            if self._task is not None and self._task.done():
                exc = self._task.exception()
                if exc is not None:
                    raise RuntimeError(f"MCP server crashed podczas startu: {exc}") from exc
                raise RuntimeError("MCP server zakończył się przed ready.")

            if self._server is not None and self._server.started:
                if await self._ping():
                    return

            now = asyncio.get_event_loop().time()
            if now >= deadline:
                raise RuntimeError(
                    f"MCP server nie wstał w {self._settings.startup_timeout_s}s "
                    f"({self._settings.url})"
                )
            await asyncio.sleep(sleep_s)
            sleep_s = min(sleep_s * 1.5, 0.25)

    async def _ping(self) -> bool:
        """Szybki GET na endpoint MCP - cokolwiek <500 znaczy "serwer żyje".

        Streamable-http endpoint odrzuca GET bez odpowiednich nagłówków
        (zwraca 4xx), ale samo to oznacza, że socket nasłuchuje i
        Starlette app odpowiada. To wystarczy jako liveness check.
        """

        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(self._settings.url)
            return resp.status_code < 500
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
            return False
        except Exception:
            return False

    async def _force_stop(self) -> None:
        """Graceful shutdown z fallbackiem na cancel."""

        if self._server is not None:
            self._server.should_exit = True

        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("MCP server task zakończył się błędem: %r", exc)

        self._started = False
        self._task = None
        self._server = None


__all__ = ["McpRuntime"]
