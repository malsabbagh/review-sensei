"""Test-only observations and fixture faults at unchanged original source frames."""

import dis
import inspect
import sys
import threading
import weakref
from contextlib import contextmanager

_monitoring = getattr(sys, "monitoring", None)
_watches: dict = {}
_tool: list = [None]
_WATCH_LOCK = threading.Lock()
_CODE_EVENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CODE_EVENTS_LOCK = threading.Lock()


def _code_events(code):
    try:
        with _CODE_EVENTS_LOCK:
            cached = _CODE_EVENTS.get(code)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    starts = []
    resumes = []
    returns = []
    yields = []
    after_yield = False
    yield_on_resume = sys.version_info[:2] == (3, 13)
    for instruction in dis.get_instructions(code):
        opname = instruction.opname
        if after_yield:
            if yield_on_resume:
                yields.append(instruction.offset)
            after_yield = False
        if opname == "RESUME":
            if instruction.arg == 0:
                starts.append(instruction.offset)
            else:
                resumes.append(instruction.offset)
        elif opname in {"RETURN_VALUE", "RETURN_CONST"}:
            returns.append(instruction.offset)
        elif opname == "YIELD_VALUE":
            if yield_on_resume:
                after_yield = True
            else:
                yields.append(instruction.offset)
    result = (
        frozenset(starts),
        frozenset(resumes),
        frozenset(returns),
        frozenset(yields),
    )
    try:
        with _CODE_EVENTS_LOCK:
            return _CODE_EVENTS.setdefault(code, result)
    except TypeError:
        return result


def _dispatch_profile_entries(entries, frame, event: str, value) -> None:
    if event != "call" and event != "return":
        return
    frame_globals = frame.f_globals
    frame_id = id(frame)
    lasti = frame.f_lasti
    for (
        namespace,
        entered,
        returned,
        yielded,
        resumed,
        starts,
        resumes,
        returns,
        yields,
        thrown_offsets,
    ) in tuple(entries):
        if namespace is not None and frame_globals is not namespace:
            continue
        if event == "call":
            if lasti in starts or (not starts and not resumes):
                thrown_offsets.pop(frame_id, None)
                if entered is not None:
                    entered(frame)
            elif lasti in resumes:
                if lasti in yields:
                    thrown_offsets[frame_id] = lasti
                else:
                    thrown_offsets.pop(frame_id, None)
                if resumed is not None:
                    resumed(frame)
            elif lasti in yields:
                thrown_offsets[frame_id] = lasti
        else:
            thrown_at = thrown_offsets.pop(frame_id, None)
            if returned is not None and lasti in returns:
                returned(frame, value)
            elif yielded is not None and lasti in yields:
                if lasti != thrown_at or value is not None:
                    yielded(frame, value)


def _make_profile_dispatcher(thread: int, previous):
    watches: dict = {}
    get_entries = watches.get
    if not callable(previous):

        def dispatch(frame, event: str, value) -> None:
            if event != "call" and event != "return":
                return
            entries = get_entries(id(frame.f_code))
            if entries is not None:
                _dispatch_profile_entries(entries, frame, event, value)

    else:

        def dispatch(frame, event: str, value) -> None:
            previous(frame, event, value)
            if event != "call" and event != "return":
                return
            entries = get_entries(id(frame.f_code))
            if entries is not None:
                _dispatch_profile_entries(entries, frame, event, value)

    dispatch._watch_thread = thread  # type: ignore[attr-defined]
    dispatch._watch_previous = previous  # type: ignore[attr-defined]
    dispatch._watch_watches = watches  # type: ignore[attr-defined]
    return dispatch


def _watched_start(code, offset):
    frame = sys._getframe(1)
    thread = threading.get_ident()
    for owner, namespace, entered, _returned, _yielded, _resumed in tuple(
        _watches.get(code, ())
    ):
        if (
            owner == thread
            and entered is not None
            and (namespace is None or frame.f_globals is namespace)
        ):
            entered(frame)


def _watched_resume(code, offset):
    frame = sys._getframe(1)
    thread = threading.get_ident()
    for owner, namespace, _entered, _returned, _yielded, resumed in tuple(
        _watches.get(code, ())
    ):
        if (
            owner == thread
            and resumed is not None
            and (namespace is None or frame.f_globals is namespace)
        ):
            resumed(frame)


def _watched_return(code, offset, value):
    frame = sys._getframe(1)
    thread = threading.get_ident()
    for owner, namespace, _entered, returned, _yielded, _resumed in tuple(
        _watches.get(code, ())
    ):
        if (
            owner == thread
            and returned is not None
            and (namespace is None or frame.f_globals is namespace)
        ):
            returned(frame, value)


def _watched_yield(code, offset, value):
    frame = sys._getframe(1)
    thread = threading.get_ident()
    for owner, namespace, _entered, _returned, yielded, _resumed in tuple(
        _watches.get(code, ())
    ):
        if (
            owner == thread
            and yielded is not None
            and (namespace is None or frame.f_globals is namespace)
        ):
            yielded(frame, value)


def _monitoring_tool():
    if _monitoring is None:
        return None
    if _tool[0] is None:
        for tool in (4, 3):
            try:
                _monitoring.use_tool_id(tool, "review-sensei-test-observation")
            except ValueError:
                continue
            events = _monitoring.events
            _monitoring.register_callback(tool, events.PY_START, _watched_start)
            _monitoring.register_callback(tool, events.PY_RESUME, _watched_resume)
            _monitoring.register_callback(tool, events.PY_RETURN, _watched_return)
            _monitoring.register_callback(tool, events.PY_YIELD, _watched_yield)
            _tool[0] = tool
            break
    return _tool[0]


def _set_watched_events(tool, code):
    events = 0
    for _owner, _namespace, entered, returned, yielded, resumed in _watches.get(
        code, ()
    ):
        if entered is not None:
            events |= _monitoring.events.PY_START
        if resumed is not None:
            events |= _monitoring.events.PY_RESUME
        if returned is not None:
            events |= _monitoring.events.PY_RETURN
        if yielded is not None:
            events |= _monitoring.events.PY_YIELD
    _monitoring.set_local_events(tool, code, events)


@contextmanager
def watch_code(
    code, *, namespace=None, entered=None, returned=None, yielded=None, resumed=None
):
    """Call back when this thread starts, resumes, yields, or returns from ``code``.

    ``entered(frame)`` runs at the start of a matching frame,
    ``resumed(frame)`` when a generator/coroutine resumes,
    ``yielded(frame, value)`` at a generator/coroutine yield, and
    ``returned(frame, value)`` at its actual return (never an exception or a
    yield). All may raise into the observed frame. With ``sys.monitoring``
    only the watched code object reports events; otherwise a chained profile
    function observes the same frames.
    """
    with _WATCH_LOCK:
        tool = _monitoring_tool()
        if tool is not None:
            watch = (
                threading.get_ident(),
                namespace,
                entered,
                returned,
                yielded,
                resumed,
            )
            _watches.setdefault(code, []).append(watch)
            _set_watched_events(tool, code)
    if tool is None:
        starts, resumes, returns, yields = _code_events(code)
        thread = threading.get_ident()
        current = sys.getprofile()
        if getattr(current, "_watch_thread", None) == thread:
            dispatcher = current
            installed = False
        else:
            dispatcher = _make_profile_dispatcher(thread, current)
            installed = True
        watches = dispatcher._watch_watches  # type: ignore[attr-defined]
        watch = (
            namespace,
            entered,
            returned,
            yielded,
            resumed,
            starts,
            resumes,
            returns,
            yields,
            {},
        )
        # The suspended context owns a strong reference to code throughout.
        # Its identity is exact and constant-time; hashing arbitrary code
        # objects in the profiler hot path can traverse their constants.
        key = id(code)
        watches.setdefault(key, []).append(watch)
        try:
            if installed:
                sys.setprofile(dispatcher)
            yield
        finally:
            entries = watches.get(key)
            if entries is not None:
                for index, item in enumerate(entries):
                    if item is watch:
                        del entries[index]
                        break
                if not entries:
                    watches.pop(key, None)
            if not watches:
                sys.setprofile(dispatcher._watch_previous)  # type: ignore[attr-defined]
        return
    try:
        yield
    finally:
        with _WATCH_LOCK:
            watches = _watches[code]
            del watches[next(i for i, item in enumerate(watches) if item is watch)]
            if not watches:
                del _watches[code]
            _set_watched_events(tool, code)
            if not _watches:
                _monitoring.free_tool_id(tool)
                _tool[0] = None


@contextmanager
def observe_original(
    function, *, entered=None, returned=None, yielded=None, resumed=None
):
    """Keep production definitions intact; invoke callbacks at exact source events.

    A return callback requires an actual return opcode, excluding exceptional
    profile-return events. Callbacks may inspect test state or inject a fixture
    fault; they do not replace a production result or grant source authority.
    """
    original = inspect.unwrap(function)
    with watch_code(
        original.__code__,
        namespace=original.__globals__,
        entered=entered,
        returned=returned,
        yielded=yielded,
        resumed=resumed,
    ):
        yield
