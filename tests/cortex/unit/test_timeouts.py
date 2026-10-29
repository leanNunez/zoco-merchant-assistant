import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

from cortex.tools.failures import PermanentFailure, TransientFailure, UncertainOutcome
from cortex.tools.timeouts import TimeLimited, TimeLimitedWrite, time_limited


class _Empty(BaseModel):
    pass


ARGS = _Empty()


class _Blocked:
    """An adapter that never answers until the test lets it."""

    tenant_id = "comercio_a"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.looked_up: list[str] = []

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        self.release.wait(timeout=5)
        return {"late": True}

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        self.looked_up.append(operation_id)
        self.release.wait(timeout=5)
        return None


class _Quick:
    tenant_id = "comercio_a"

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        return {"conversation": conversation_id}


class _Refusing(_Quick):
    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        raise PermanentFailure("no")


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=2)
    yield pool
    pool.shutdown(wait=True, cancel_futures=True)


def test_an_attempt_inside_its_time_returns_its_data(executor: ThreadPoolExecutor) -> None:
    limited = TimeLimited(_Quick(), executor, lambda: 1000)

    assert limited(ARGS, conversation_id="conv_1") == {"conversation": "conv_1"}
    assert limited.tenant_id == "comercio_a"


def test_a_slow_read_is_abandoned_as_a_transient_failure(executor: ThreadPoolExecutor) -> None:
    blocked = _Blocked()
    limited = TimeLimited(blocked, executor, lambda: 30)

    with pytest.raises(TransientFailure):
        limited(ARGS, conversation_id="conv_1")

    blocked.release.set()


def test_a_slow_write_is_an_uncertain_outcome_never_a_plain_failure(
    executor: ThreadPoolExecutor,
) -> None:
    blocked = _Blocked()
    limited = TimeLimitedWrite(blocked, executor, lambda: 30)

    with pytest.raises(UncertainOutcome):
        limited(ARGS, conversation_id="conv_1")
    with pytest.raises(UncertainOutcome):  # the lookup that reconciles has a limit too
        limited.lookup("op_1")

    blocked.release.set()
    assert blocked.looked_up == ["op_1"]


def test_with_no_time_left_nothing_is_even_started(executor: ThreadPoolExecutor) -> None:
    blocked = _Blocked()
    limited = TimeLimited(blocked, executor, lambda: 0)

    with pytest.raises(TransientFailure):
        limited(ARGS, conversation_id="conv_1")

    blocked.release.set()


def test_the_adapters_own_failures_pass_through_unchanged(executor: ThreadPoolExecutor) -> None:
    limited = TimeLimited(_Refusing(), executor, lambda: 1000)

    with pytest.raises(PermanentFailure):
        limited(ARGS, conversation_id="conv_1")


def test_a_timeout_raised_by_the_adapter_itself_counts_too(executor: ThreadPoolExecutor) -> None:
    class Timing(_Quick):
        def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
            raise TimeoutError("the upstream timed out")

    with pytest.raises(TransientFailure):
        TimeLimited(Timing(), executor, lambda: 1000)(ARGS, conversation_id="conv_1")


def test_the_wrapper_matches_the_kind_of_the_adapter(executor: ThreadPoolExecutor) -> None:
    assert type(time_limited(_Quick(), executor, lambda: 1)) is TimeLimited
    assert type(time_limited(_Blocked(), executor, lambda: 1)) is TimeLimitedWrite
