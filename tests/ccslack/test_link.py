import asyncio

import pytest

from ccslack import link


class _BufWriter:
    """Minimal StreamWriter stand-in: collects bytes for write_msg."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        return None


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_write_then_read_roundtrip():
    writer = _BufWriter()
    await link.write_msg(writer, link.event({"a": 1}))
    await link.write_msg(writer, link.hello("gpu1", ["C1", "C2"]))

    reader = _reader(bytes(writer.buf))
    assert await link.read_msg(reader) == {"t": "event", "payload": {"a": 1}}
    assert await link.read_msg(reader) == {
        "t": "hello",
        "host": "gpu1",
        "channels": ["C1", "C2"],
    }
    assert await link.read_msg(reader) is None  # EOF after the two frames


@pytest.mark.asyncio
async def test_read_eof_returns_none():
    assert await link.read_msg(_reader(b"")) is None


@pytest.mark.asyncio
async def test_read_malformed_line_returns_empty_dict():
    assert await link.read_msg(_reader(b"not json\n")) == {}
