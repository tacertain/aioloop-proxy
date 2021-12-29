import asyncio
import concurrent.futures
import threading
import warnings
import weakref

from ._handle import _ProxyHandle, _ProxyTimerHandle
from ._protocol import _proto_proxy, _proto_proxy_factory
from ._server import _ServerProxy
from ._transport import _make_transport_proxy


class LoopProxy(asyncio.AbstractEventLoop):
    def __init__(self, parent) -> None:
        assert isinstance(parent, asyncio.AbstractEventLoop)
        self._parent = parent
        self._closed = False

        self._handles = weakref.WeakSet()
        self._timers = weakref.WeakSet()
        self._readers = {}
        self._writers = {}
        self._futures = weakref.WeakSet()
        self._tasks = weakref.WeakSet()
        self._transports = weakref.WeakSet()
        self._servers = weakref.WeakSet()
        self._signals = {}

        self._task_factory = None
        self._default_executor = None
        self._executor_shutdown_called = False

        # shared state, need to restore parent loop
        self._debug = parent.get_debug()
        self._exception_handler = parent.get_exception_handler()
        self._slow_callback_duration = parent.slow_callback_duration

    # properties
    @property
    def slow_callback_duration(self):
        return self._slow_callback_duration

    @slow_callback_duration.setter
    def slow_callback_duration(self, value):
        self._slow_callback_duration = value
        self._parent.slow_callback_duration = value

    # Proxy-specific API

    def check_resouces(self, *, strict=None):
        if strict is None:
            strict = self.get_debug()

    # Running and stopping the event loop.

    def run_forever(self) -> None:
        self._check_closed()
        self._parent.run_forever()

    def run_until_complete(self, coro_or_future):
        async def main():
            new_task = not asyncio.isfuture(coro_or_future)
            future = asyncio.ensure_future(coro_or_future, loop=self)
            if new_task:
                # An exception is raised if the future didn't complete,
                # so there
                # is no need to log the "destroy pending task" message
                future._log_destroy_pending = False
            waiter = self._parent.create_future()
            self._chain_future(waiter, future)
            ret = await waiter
            return ret

        return self._parent.run_until_complete(main())

    def stop(self):
        return self._parent.stop()

    def is_running(self):
        return self._parent.is_running()

    def is_closed(self):
        return self._closed

    def close(self):
        if self.is_running():
            raise RuntimeError("Cannot close a running event loop")
        self._closed = True
        self._executor_shutdown_called = True
        executor = self._default_executor
        if executor is not None:
            warnings.warn(
                "Please call 'await proxy.shutdown_default_executor() explicitly "
                "before 'loop.close()' call.",
                RuntimeWarning,
            )
            self._default_executor = None
            executor.shutdown(wait=False)

    async def shutdown_asyncgens(self):
        warnings.warn(
            "Only original loop can shutdown async generators", RuntimeWarning
        )
        return

    async def shutdown_default_executor(self):
        self._executor_shutdown_called = True
        if self._default_executor is None:
            return
        future = self.create_future()
        thread = threading.Thread(target=self._do_shutdown, args=(future,))
        thread.start()
        try:
            await future
        finally:
            thread.join()

    def _do_shutdown(self, future):
        try:
            self._default_executor.shutdown(wait=True)
            self.call_soon_threadsafe(future.set_result, None)
        except Exception as ex:
            self.call_soon_threadsafe(future.set_exception, ex)
        finally:
            self._default_executor = None

    # Methods scheduling callbacks.  All these return Handles.

    def call_soon(self, callback, *args, context=None):
        self._check_closed()
        handle = _ProxyHandle(callback, args, self, context)
        parent_handle = self._wrap_sync(
            self._parent.call_soon, self._run_handle, handle
        )
        handle._parent = parent_handle
        if handle._source_traceback:
            del handle._source_traceback[-1]
        self._handles.add(handle)
        return handle

    def call_later(self, delay, callback, *args, context=None):
        self._check_closed()
        timer = _ProxyTimerHandle(self.time() + delay, callback, args, self, context)
        parent_timer = self._wrap_sync(
            self._parent.call_later, delay, self._run_handle, timer
        )
        timer._parent = parent_timer
        if timer._source_traceback:
            del timer._source_traceback[-1]
        self._timers.add(timer)
        return timer

    def call_at(self, when, callback, *args, context=None):
        self._check_closed()
        timer = _ProxyTimerHandle(when, callback, args, self, context)
        parent_timer = self._wrap_sync(
            self._parent.call_at, when, self._run_handle, timer
        )
        timer._parent = parent_timer
        if timer._source_traceback:
            del timer._source_traceback[-1]
        self._timers.add(timer)
        return timer

    def time(self):
        return self._parent.time()

    def create_future(self):
        fut = asyncio.Future(loop=self)
        self._futures.add(fut)
        return fut

    # Method scheduling a coroutine object: create a task.

    def create_task(self, coro, *, name=None):
        self._check_closed()
        if self._task_factory is None:
            task = asyncio.Task(coro, loop=self, name=name)
            if task._source_traceback:
                del task._source_traceback[-1]
        else:
            task = self._task_factory(self, coro)
            if name is not None:
                try:
                    set_name = task.set_name
                except AttributeError:
                    pass
                else:
                    set_name(name)
        self._tasks.add(task)
        return task

    # Methods for interacting with threads.

    def call_soon_threadsafe(self, callback, *args, context=None):
        self._check_closed()
        handle = _ProxyHandle(callback, args, self, context)
        parent_handle = self._wrap_sync(
            self._parent.call_soon_threadsafe, self._run_handle, handle
        )
        handle._parent = parent_handle
        if handle._source_traceback:
            del handle._source_traceback[-1]
        self._handles.add(handle)
        return handle

    def run_in_executor(self, executor, func, *args):
        self._check_closed()
        if executor is None:
            executor = self._default_executor
            # Only check when the default executor is being used
            self._check_default_executor()
            if executor is None:
                executor = concurrent.futures.ThreadPoolExecutor(
                    thread_name_prefix="aioloop-proxy"
                )
                self._default_executor = executor
        parent_fut = self._wrap_sync(
            self._parent.run_in_executor, executor, func, *args
        )
        fut = self.create_future()
        self._chain_future(fut, parent_fut)
        self._futures.add(fut)
        return fut

    def set_default_executor(self, executor):
        if not isinstance(executor, concurrent.futures.ThreadPoolExecutor):
            raise TypeError("executor must be ThreadPoolExecutor instance")
        self._default_executor = executor

    # Network I/O methods returning Futures.

    async def getaddrinfo(self, host, port, **kwargs):
        self._check_closed()
        return await self._wrap_async(self._parent.getaddrinfo(host, port, **kwargs))

    async def getnameinfo(self, sockaddr, flags=0):
        self._check_closed()
        return await self._wrap_async(self._parent.getnameinfo(sockaddr, flags))

    async def create_connection(self, protocol_factory, host=None, port=None, **kwargs):
        self._check_closed()
        _, proto = await self._wrap_async(
            self._parent.create_connection(
                _proto_proxy_factory(protocol_factory, self), host, port, **kwargs
            )
        )
        transp = proto.transport
        self._transports.add(transp)
        return transp, proto.protocol

    async def create_server(self, protocol_factory, host=None, port=None, **kwargs):
        self._check_closed()
        server = await self._wrap_async(
            self._parent.create_server(
                _proto_proxy_factory(protocol_factory, self), host, port, **kwargs
            )
        )
        server = _ServerProxy(server, self)
        self._servers.add(server)
        return server

    async def sendfile(self, transport, file, offset=0, count=None, *, fallback=True):
        self._check_closed()
        sent_count = await self._wrap_async(
            self._parent.sendfile(
                transport._orig, file, offset, count, fallback=fallback
            )
        )
        return sent_count

    async def start_tls(self, transport, protocol, sslcontext, **kwargs):
        self._check_closed()
        proto = _proto_proxy(protocol, self)
        tr = await self._wrap_async(
            self._parent.start_tls(transport._orig, proto, sslcontext, **kwargs)
        )
        transp = _make_transport_proxy(tr, self)
        proto.transport = transp
        self._transports.add(transp)
        return transp

    async def create_unix_connection(self, protocol_factory, path=None, **kwargs):
        self._check_closed()
        _, proto = await self._wrap_async(
            self._parent.create_unix_connection(
                _proto_proxy_factory(protocol_factory, self), path, **kwargs
            )
        )
        transp = proto.transport
        self._transports.add(transp)
        return transp, proto.protocol

    async def create_unix_server(self, protocol_factory, path=None, **kwargs):
        self._check_closed()
        server = await self._wrap_async(
            self._parent.create_unix_server(
                _proto_proxy_factory(protocol_factory, self), path, **kwargs
            )
        )
        server = _ServerProxy(server, self)
        self._servers.add(server)
        return server

    async def connect_accepted_socket(self, protocol_factory, sock, **kwargs):
        self._check_closed()
        _, proto = await self._wrap_async(
            self._parent.connect_accepted_socket(
                _proto_proxy_factory(protocol_factory, self), sock, **kwargs
            )
        )
        transp = proto.transport
        self._transports.add(transp)
        return transp, proto.protocol

    async def create_datagram_endpoint(
        self, protocol_factory, local_addr=None, remote_addr=None, **kwargs
    ):
        self._check_closed()
        _, proto = await self._wrap_async(
            self._parent.create_datagram_endpoint(
                _proto_proxy_factory(protocol_factory, self),
                local_addr,
                remote_addr,
                **kwargs,
            )
        )
        transp = proto.transport
        self._transports.add(transp)
        return transp, proto.protocol

    # Pipes and subprocesses.

    async def connect_read_pipe(self, protocol_factory, pipe):
        self._check_closed()
        _, proto = await self._wrap_async(
            self._parent.connect_read_pipe(
                _proto_proxy_factory(protocol_factory, self), pipe
            )
        )
        transp = proto.transport
        self._transports.add(transp)
        return transp, proto.protocol

    async def connect_write_pipe(self, protocol_factory, pipe):
        self._check_closed()
        _, proto = await self._wrap_async(
            self._parent.connect_write_pipe(
                _proto_proxy_factory(protocol_factory, self), pipe
            )
        )
        transp = proto.transport
        self._transports.add(transp)
        return transp, proto.protocol

    async def subprocess_shell(self, protocol_factory, cmd, **kwargs):
        self._check_closed()
        transp, proto = await self._wrap_async(
            self._parent.subprocess_shell(
                _proto_proxy_factory(protocol_factory, self), cmd, **kwargs
            )
        )
        self._transports.add(transp)
        return transp, proto

    async def subprocess_exec(self, protocol_factory, *args, **kwargs):
        self._check_closed()
        transp, proto = await self._wrap_async(
            self._parent.subprocess_exec(
                _proto_proxy_factory(protocol_factory, self), *args, **kwargs
            )
        )
        self._transports.add(transp)
        return transp, proto

    # Ready-based callback registration methods.
    # The add_*() methods return None.
    # The remove_*() methods return True if something was removed,
    # False if there was nothing to delete.

    def add_reader(self, fd, callback, *args):
        self._check_closed()
        handle = _ProxyHandle(callback, args, self)
        parent_handle = self._wrap_sync(
            self._parent.add_reader, fd, self._run_handle, handle
        )
        handle._parent = parent_handle
        if handle._source_traceback:
            del handle._source_traceback[-1]
        self._readers[fd] = handle

    def remove_reader(self, fd):
        if self.is_closed():
            return False
        parent_ret = self._wrap_sync(self._parent.remove_reader, fd)
        handle = self._readers.pop(fd, None)
        if handle is not None:
            handle.cancel()
            assert parent_ret, f"Parent loop already removed a reader for {fd}"
            return True
        else:
            assert parent_ret, f"Parent loop has no reader for {fd}"
            return False

    def add_writer(self, fd, callback, *args):
        self._check_closed()
        handle = _ProxyHandle(callback, args, self)
        parent_handle = self._wrap_sync(
            self._parent.add_writer, fd, self._run_handle, handle
        )
        handle._parent = parent_handle
        if handle._source_traceback:
            del handle._source_traceback[-1]
        self._writers[fd] = handle

    def remove_writer(self, fd):
        if self.is_closed():
            return False
        parent_ret = self._wrap_sync(self._parent.remove_writer, fd)
        handle = self._writers.pop(fd, None)
        if handle is not None:
            handle.cancel()
            assert parent_ret, f"Parent loop already removed a writer for {fd}"
            return True
        else:
            assert parent_ret, f"Parent loop has no writer for {fd}"
            return False

    # Completion based I/O methods returning Futures.

    async def sock_recv(self, sock, nbytes):
        self._check_closed()
        return await self._wrap_async(self._parent.sock_recv(sock, nbytes))

    async def sock_recv_into(self, sock, buf):
        self._check_closed()
        return await self._wrap_async(self._parent.sock_recv_into(sock, buf))

    async def sock_sendall(self, sock, data):
        self._check_closed()
        return await self._wrap_async(self._parent.sock_sendall(sock, data))

    async def sock_connect(self, sock, address):
        self._check_closed()
        return await self._wrap_async(self._parent.sock_connect(sock, address))

    async def sock_accept(self, sock):
        self._check_closed()
        return await self._wrap_async(self._parent.sock_accept(sock))

    async def sock_sendfile(self, sock, file, offset=0, count=None, *, fallback=None):
        self._check_closed()
        return await self._wrap_async(
            self._parent.sock_sendfile(sock, file, offset, count, fallback=fallback)
        )

    # Signal handling.

    def add_signal_handler(self, sig, callback, *args):
        self._check_closed()
        handle = _ProxyHandle(callback, args, self)
        parent_handle = self._wrap_sync(
            self._parent.add_signal_handler, sig, self._run_handle, handle
        )
        handle._parent = parent_handle
        if handle._source_traceback:
            del handle._source_traceback[-1]
        self._signals[sig] = handle

    def remove_signal_handler(self, sig):
        handler = self._signals.pop(sig, None)
        removed_by_parent = self._wrap_sync(self._parent.remove_signal_handler, sig)
        # ignore removed_by_parent
        # don't touch if did not set
        removed_by_parent
        return handler is not None

    # Task factory.

    def set_task_factory(self, factory):
        if factory is not None and not callable(factory):
            raise TypeError("task factory must be a callable or None")
        self._task_factory = factory

    def get_task_factory(self):
        return self._task_factory

    # Error handlers.

    def get_exception_handler(self):
        return self._exception_handler

    def set_exception_handler(self, handler):
        self._parent.set_exception_handler(handler)
        self._exception_handler = handler

    def default_exception_handler(self, context):
        self._parent.default_exception_handler(context)

    def call_exception_handler(self, context):
        self._parent.call_exception_handler(context)

    # Debug flag management.

    def get_debug(self):
        return self._debug

    def set_debug(self, enabled):
        self._debug = enabled
        self._parent.set_debug(enabled)

    # Inherited

    def _timer_handle_cancelled(self, handle):
        # Nothing to do, _ProxyTimerHandle.cancel()
        # already cancelled the parent timer
        pass

    # Implementation details

    def _check_closed(self):
        if self._closed:
            raise RuntimeError("Event loop is closed")

    def _check_default_executor(self):
        if self._executor_shutdown_called:
            raise RuntimeError("Executor shutdown has been called")

    def _wrap_sync(self, __func, *args, **kwargs):
        # Private API calls are OK here
        loop = asyncio._get_running_loop()
        asyncio._set_running_loop(self._parent)
        try:
            return __func(*args, **kwargs)
        finally:
            asyncio._set_running_loop(loop)

    def _wrap_sync_proto(self, __func, *args, **kwargs):
        # Private API calls are OK here
        loop = asyncio._get_running_loop()
        asyncio._set_running_loop(self)
        try:
            return __func(*args, **kwargs)
        finally:
            asyncio._set_running_loop(loop)

    def _wrap_async(self, coro_or_future):
        # Private API calls are OK here
        loop = asyncio._get_running_loop()
        assert loop is None or loop is self
        fut = self.create_future()
        coro_or_future = asyncio.ensure_future(coro_or_future, loop=self._parent)
        self._chain_future(fut, coro_or_future)
        return fut

    def _chain_future(self, target, source):
        def _call_check_cancel(target):
            if target.cancelled():
                source.cancel()

        def _call_set_state(source):
            if target._loop.is_closed():
                return
            if source.cancelled():
                target.cancel()
                return
            exc = source.exception()
            if exc is not None:
                target.set_exception(exc)
            else:
                res = source.result()
                target.set_result(res)

        target.add_done_callback(_call_check_cancel)
        source.add_done_callback(_call_set_state)

    def _run_handle(self, handle):
        # Private API calls are OK here
        loop = asyncio._get_running_loop()
        asyncio._set_running_loop(self)
        try:
            handle._run()
        finally:
            # handle._run() should never throw and exception
            # except SystemExit and KeyboardInterrupt

            # Break circle reference for handled callbacks
            handle._parent = None
            asyncio._set_running_loop(loop)
