from __future__ import annotations

import inspect
import warnings
from enum import IntEnum
from types import TracebackType
from typing import (
    Any,
    Awaitable,
    Generic,
    Mapping,
    Optional,
    Protocol,
    Type,
    TypeVar,
    Union,
)

from aio.exceptions import Cancelled, FutureFinishedError, FutureNotReady
from aio.interfaces import EventLoop
from aio.loop import _get_loop_inner

T = TypeVar('T')


class FutureResultCallback(Protocol[T]):
    def __call__(self, fut: Future[T]) -> None:
        raise NotImplementedError


class Promise(Protocol[T]):
    def set_result(self, val: T) -> None:
        raise NotImplementedError

    def set_exception(self, exc: Exception) -> None:
        raise NotImplementedError

    def cancel(self, msg: Optional[str] = None) -> None:
        raise NotImplementedError

    @property
    def future(self) -> Future[T]:
        raise NotImplementedError


class _FuturePromise(Promise[T]):
    def __init__(self, future: Future[T]):
        self._fut = future

    def set_result(self, val: T) -> None:
        self._fut._set_result(val=val)

    def set_exception(self, exc: Exception) -> None:
        if isinstance(exc, Cancelled):
            raise ValueError(
                f'Use cancellation API instead of passing exception '
                f'`{Cancelled.__module__}.{Cancelled.__qualname__}` manually'
            )
        self._fut._set_result(exc=exc)

    def cancel(self, msg: Optional[str] = None) -> None:
        self._fut._cancel(msg)

    @property
    def future(self) -> Future[T]:
        return self._fut


class _NotSet:
    pass


_not_set = _NotSet()


class Future(Generic[T]):
    class State(IntEnum):
        created = 0
        scheduled = 1
        running = 2
        finishing = 3
        finished = 4

    def __init__(self, loop: EventLoop, label: str = None, **context: Any):
        self._value: Union[Optional[T], _NotSet] = _not_set
        self._exc: Union[Optional[Exception], _NotSet] = _not_set
        self._label = label
        self._context: Mapping[str, Any] = {
            'future': self,
            'future_label': label,
            **context,
        }

        self._result_callbacks: set[FutureResultCallback] = set()
        self._state = Future.State.running

        self._loop = loop

    @property
    def state(self) -> Future.State:
        return self._state

    @property
    def subscribers_count(self) -> int:
        return len(self._result_callbacks)

    @property
    def _result(self) -> Optional[T]:
        return self._value if not isinstance(self._value, _NotSet) else None

    @property
    def _exception(self) -> Optional[Exception]:
        return self._exc if not isinstance(self._exc, _NotSet) else None

    @property
    def result(self) -> T:
        if not self.is_finished():
            raise FutureNotReady
        if self._exception is not None:
            raise self._exception
        assert not isinstance(self._value, _NotSet)
        return self._value

    @property
    def exception(self) -> Optional[Exception]:
        if not self.is_finished():
            raise FutureNotReady
        return self._exception

    def add_callback(self, cb: FutureResultCallback) -> None:
        if cb in self._result_callbacks:
            return

        if self.is_finished():
            self._schedule_cb(cb)
            return

        self._result_callbacks.add(cb)

    def remove_callback(self, cb: FutureResultCallback) -> None:
        try:
            self._result_callbacks.remove(cb)
        except ValueError:
            pass

    def is_finished(self) -> bool:
        finished = self._value is not _not_set or self._exc is not _not_set
        assert (
            not finished or self._state == Future.State.finished
        ), 'Future has inconsistent state'
        return finished

    def is_cancelled(self) -> bool:
        return self.is_finished() and isinstance(self._exc, Cancelled)

    def _call_cbs(self) -> None:
        assert self.is_finished(), 'Future must finish before calling callbacks'

        for cb in self._result_callbacks:
            self._schedule_cb(cb)

    def _schedule_cb(self, cb: FutureResultCallback) -> None:
        assert self.is_finished(), 'Future must finish before scheduling callbacks'

        self._loop.call_soon(cb, self, context=self._context)

    def _set_result(
        self,
        val: Union[T, _NotSet] = _not_set,
        exc: Union[Exception, _NotSet] = _not_set,
    ) -> None:
        if self.is_finished():
            raise FutureFinishedError

        self._state = Future.State.finished

        self._value = val
        self._exc = exc
        self._call_cbs()

    def _cancel(self, msg: Optional[str] = None) -> None:
        self._set_result(exc=Cancelled(msg))

    def __await__(self) -> T:
        if not self.is_finished():
            yield self

        if not self.is_finished():
            raise FutureNotReady('The future object resumed before result has been set')

        if self._exception:
            raise self._exception

        assert (
            self._value is not _not_set
        ), 'Value and exception mutually exclusive when `is_finished` returns True'
        return self._value

    def __repr__(self) -> str:
        label = self._label if self._label else ''
        return f'<Future label="{label}" state={self._state.name} at {hex(id(self))}>'

    def __eq__(self, other: Any) -> Union[NotImplemented, bool]:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self is other

    def __del__(self) -> None:
        if not self.is_finished():
            warnings.warn(
                (
                    f'Feature `{self}` is about to be destroyed, but not finished, '
                    f'that normally should never occur '
                    f'(feature context `{self._context}`)'
                ),
                stacklevel=2,
            )


class Coroutine(Awaitable[T], Protocol[T]):
    """Loop bound coroutine type"""

    def send(self, value: None) -> Future[Any]:
        raise NotImplementedError

    def throw(
        self,
        typ: Type[BaseException],
        val: Union[BaseException, object, None] = None,
        tb: Optional[TracebackType] = None,
    ) -> Future[Any]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class Task(Future[T]):
    def __init__(
        self,
        coroutine: Coroutine[T],
        loop: EventLoop,
        label: Optional[str] = None,
    ):
        if not inspect.iscoroutine(coroutine):
            raise TypeError(f'Coroutine object is expected, not `{coroutine}`')

        super().__init__(loop, label, task=self, future=None)
        self._coroutine = coroutine
        self._waiting_on: Optional[Future[Any]] = None
        self._pending_cancellation = False
        self._state = Future.State.created
        self._started_promise = _create_promise(
            f'task-started-future', _loop=loop, served_task=self
        )

    def _cancel(self, msg: Optional[str] = None) -> None:
        if self.is_finished():
            raise FutureFinishedError

        self._state = Future.State.finishing

        if self._waiting_on is not None:
            # Recursively cancel all inner tasks
            self._waiting_on._cancel(msg)
        else:
            super()._cancel(msg)

    def _schedule_execution(self, _: Optional[Future[Any]] = None) -> None:
        self._loop.call_soon(self._execute_coroutine_step)
        if self._state == Future.State.created:
            self._state = Future.State.scheduled

    def _execute_coroutine_step(self) -> None:
        try:
            future = self._coroutine.send(None)
        except StopIteration as exc:
            # `exc.value` must be instance of `T`,
            # but there is no way we could check that
            val: T = exc.value
            self._set_result(val=val)
            return
        except Exception as exc:
            self._set_result(exc=exc)
            return

        if self._state == Future.State.scheduled:
            self._state = Future.State.running
            self._started_promise.set_result(None)

        if future is self:
            raise RuntimeError(
                'Task awaiting on itself, this will cause '
                'infinity awaiting that\'s why is forbidden'
            )

        if self._waiting_on:
            self._waiting_on.remove_callback(self._schedule_execution)

        assert isinstance(future, Future)
        if isinstance(future, Task) and future._loop is not self._loop:
            raise RuntimeError(
                f'During processing task "{self!r}" another '
                f'task has been "{future!r}" received, which '
                f"does not belong to the same loop"
            )

        future.add_callback(self._schedule_execution)
        self._waiting_on = future

    def _set_result(
        self,
        val: Union[T, _NotSet] = _not_set,
        exc: Union[Exception, _NotSet] = _not_set,
    ) -> None:
        super()._set_result(val, exc)
        if not self._started_promise.future.is_finished():
            self._started_promise.set_result(None)

    def __repr__(self) -> str:
        return f'<Task state={self._state.name} for {self._coroutine!r}>'


def shield(future: Future[T]) -> Future[T]:
    shield_promise: Promise[T] = _create_promise()

    def future_done_cb(_: Future[T]) -> None:
        assert future.is_finished()

        if future.exception:
            shield_promise.set_exception(future.exception)
        else:
            shield_promise.set_result(future.result)

    future.add_callback(future_done_cb)

    return shield_promise.future


def _create_promise(
    label: str = None, *, _loop: Optional[EventLoop] = None, **context: Any
) -> Promise[T]:
    if _loop is None:
        _loop = _get_loop_inner()
    future: Future[T] = Future(_loop, label=label, **context)
    return _FuturePromise(future)


def _create_task(coro: Coroutine[T], *, _loop: Optional[EventLoop] = None) -> Task[T]:
    if _loop is None:
        _loop = _get_loop_inner()
    task = Task(coro, _loop)
    task._schedule_execution()
    return task