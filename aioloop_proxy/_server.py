from __future__ import annotations

import asyncio
import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._loop import LoopProxy


class _ServerProxy(asyncio.AbstractServer):
    def __init__(self, original: asyncio.AbstractServer, loop: LoopProxy):
        self._orig = original
        self._loop = loop

    def __repr__(self) -> str:
        return repr(self._orig)

    def get_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def is_serving(self) -> bool:
        return self._orig.is_serving()

    @property
    def sockets(self) -> list[socket.socket]:
        return self._orig.sockets  # type: ignore

    def close(self) -> None:
        return self._orig.close()

    async def start_serving(self) -> None:
        await self._loop._wrap_async(self._orig.start_serving())

    async def serve_forever(self) -> None:
        await self._loop._wrap_async(self._orig.serve_forever())

    async def wait_closed(self) -> None:
        await self._loop._wrap_async(self._orig.wait_closed())

    if sys.version_info >= (3, 13):

        def close_clients(self) -> None:
            self._loop._wrap_cb(self._orig.close_clients)

        def abort_clients(self) -> None:
            self._loop._wrap_cb(self._orig.abort_clients)
