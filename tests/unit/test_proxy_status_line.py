from __future__ import annotations

import runpy
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "tests/integration/test_reference_proxy_policy.py"


class FakeConnection:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = iter(chunks)

    def recv(self, _size: int) -> bytes:
        return next(self.chunks, b"")

    def settimeout(self, _timeout: float) -> None:
        pass


def test_reads_fragmented_status_line() -> None:
    read_status = runpy.run_path(str(MODULE))["_read_status_line"]
    assert (
        read_status(FakeConnection(b"HTTP/1.", b"1 403 Forbidden\r", b"\n"), time.monotonic() + 1)
        == 403
    )


def test_empty_response_can_be_retried_as_not_ready() -> None:
    read_status = runpy.run_path(str(MODULE))["_read_status_line"]
    assert read_status(FakeConnection(b""), time.monotonic() + 1) is None


def test_expired_deadline_fails() -> None:
    read_status = runpy.run_path(str(MODULE))["_read_status_line"]
    with pytest.raises(TimeoutError):
        read_status(FakeConnection(b"HTTP/1.1 200 OK\r\n"), time.monotonic() - 1)


@pytest.mark.parametrize("chunks", [(b"HTTP/1.1 403", b""), (b"invalid\r\n",), (b"x" * 8193,)])
def test_incomplete_invalid_or_oversized_response_fails(chunks: tuple[bytes, ...]) -> None:
    read_status = runpy.run_path(str(MODULE))["_read_status_line"]
    with pytest.raises(ValueError):
        read_status(FakeConnection(*chunks), time.monotonic() + 1)
