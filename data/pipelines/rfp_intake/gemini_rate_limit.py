"""Distributed reservation limiter for TrackFlow RFP Gemini requests."""

import os
import time

import redis


RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local spacing = tonumber(ARGV[2])
local next_allowed = tonumber(redis.call('GET', KEYS[1]) or '0')

local reserved = math.max(now, next_allowed)
local following = reserved + spacing

redis.call('SET', KEYS[1], following, 'PX', math.ceil(following - now + spacing))

return reserved
"""


class GeminiRateLimitUnavailable(RuntimeError):
    """Redis could not coordinate Gemini request reservations."""


def wait_for_gemini_slot(client=None):
    """Reserve a shared request slot and wait until it becomes available."""

    rpm = int(os.getenv("RFP_GEMINI_RPM", "10"))

    if rpm < 1 or rpm > 60:
        raise ValueError("RFP_GEMINI_RPM must be between 1 and 60")

    spacing_ms = 60000 // rpm

    if client is None:
        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    try:
        reserved = int(
            client.eval(
                RESERVE_SCRIPT,
                1,
                "trackflow:rfp:gemini:next",
                int(time.time() * 1000),
                spacing_ms,
            )
        )
    except redis.RedisError as exc:
        raise GeminiRateLimitUnavailable(
            "Gemini reservation unavailable"
        ) from exc

    delay = max(0, (reserved - int(time.time() * 1000)) / 1000)

    if delay:
        time.sleep(delay)

    return delay
