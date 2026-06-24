"""Router↔worker link protocol: newline-delimited JSON over a stream.

The stream is an SSH-forwarded TCP connection. The **worker** runs the server;
the **router** connects (through the tunnel) as the client. Message kinds:

  worker → router:
    {"t": "hello",  "host": <str>, "channels": [<channel_id>, ...]}  # on connect
    {"t": "bind",   "channel": <channel_id>}     # a session was (re)bound here
    {"t": "unbind", "channel": <channel_id>}     # a session ended here
    {"t": "pong"}
  router → worker:
    {"t": "event",  "payload": {<raw Slack event>}}   # dispatch into the app
    {"t": "ping"}

Framing is one JSON object per line. Both ends use :func:`read_msg` /
:func:`write_msg`; malformed lines are skipped (returned as ``{}``) rather than
killing the connection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Message-type tags.
HELLO = "hello"
BIND = "bind"
UNBIND = "unbind"
EVENT = "event"
PING = "ping"
PONG = "pong"


async def write_msg(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """Write one framed JSON message and flush."""
    writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))
    await writer.drain()


async def read_msg(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one framed JSON message. ``None`` on EOF; ``{}`` on a malformed line."""
    line = await reader.readline()
    if not line:
        return None
    try:
        decoded = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def hello(host: str, channels: list[str]) -> dict[str, Any]:
    return {"t": HELLO, "host": host, "channels": list(channels)}


def event(payload: dict[str, Any]) -> dict[str, Any]:
    return {"t": EVENT, "payload": payload}


__all__ = [
    "BIND",
    "EVENT",
    "HELLO",
    "PING",
    "PONG",
    "UNBIND",
    "event",
    "hello",
    "read_msg",
    "write_msg",
]
