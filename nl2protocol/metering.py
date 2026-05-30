"""
metering.py — Per-run token + latency accounting for the Anthropic client.

`MeteredClient` is a proxy around an Anthropic client. Every
`messages.create` call is timed and its `response.usage` accumulated
into the supplied `RunMeter`. The current pipeline stage is read from
a contextvar set by `MetricsReporter` on `stage_start` events — calls
issued during that stage are attributed to it in the per-stage rollup.

Thread-safety:
- `RunMeter.record_call` takes a `threading.Lock` so concurrent
  LLM calls (or future parallel suggesters) never race on counters.
- `current_stage` is a `contextvars.ContextVar`. Python contracts each
  thread / asyncio task sees its own value, so the per-stage attribution
  stays correct even when multiple pipelines run on different threads.

Why a proxy and not a subclass: the Anthropic SDK's `Anthropic` class
is not designed for inheritance — its `__init__` does a lot of
construction. A proxy that owns a wrapped `messages` attribute and
delegates everything else via `__getattr__` keeps us decoupled from
SDK internals.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict


NO_STAGE = "(no-stage)"

current_stage: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nl2protocol_current_stage", default=NO_STAGE
)


@dataclass
class StageStats:
    """Per-stage rollup. `wall_clock_s` is filled by MetricsReporter
    from stage_start/stage_complete events; the rest come from
    `RunMeter.record_call`."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    api_duration_s: float = 0.0
    wall_clock_s: float = 0.0


@dataclass
class RunMeter:
    """Accumulates token + latency stats for one pipeline run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    api_duration_s: float = 0.0
    per_stage: Dict[str, StageStats] = field(
        default_factory=lambda: defaultdict(StageStats)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_call(
        self,
        stage: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
        duration_s: float,
    ) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cache_read_tokens += cache_read_tokens
            self.cache_creation_tokens += cache_creation_tokens
            self.api_duration_s += duration_s
            s = self.per_stage[stage]
            s.calls += 1
            s.input_tokens += input_tokens
            s.output_tokens += output_tokens
            s.cache_read_tokens += cache_read_tokens
            s.cache_creation_tokens += cache_creation_tokens
            s.api_duration_s += duration_s

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable copy of current totals + per-stage rollup."""
        with self._lock:
            return {
                "totals": {
                    "calls": self.calls,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "cache_read_tokens": self.cache_read_tokens,
                    "cache_creation_tokens": self.cache_creation_tokens,
                    "api_duration_s": round(self.api_duration_s, 3),
                },
                "per_stage": {
                    stage: {
                        "calls": s.calls,
                        "input_tokens": s.input_tokens,
                        "output_tokens": s.output_tokens,
                        "cache_read_tokens": s.cache_read_tokens,
                        "cache_creation_tokens": s.cache_creation_tokens,
                        "api_duration_s": round(s.api_duration_s, 3),
                        "wall_clock_s": round(s.wall_clock_s, 3),
                    }
                    for stage, s in self.per_stage.items()
                },
            }


class _MeteredMessages:
    """Proxy around `client.messages`. Only `create` is timed; other
    attributes (`stream`, `count_tokens`, etc.) pass through."""

    def __init__(self, inner: Any, meter: RunMeter):
        self._inner = inner
        self._meter = meter

    def create(self, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        resp = self._inner.create(**kwargs)
        dt = time.perf_counter() - t0
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._meter.record_call(
                stage=current_stage.get(),
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                duration_s=dt,
            )
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class MeteredClient:
    """Drop-in proxy for an Anthropic client that meters every
    `messages.create`. Other attributes (`.beta`, `.with_options`, ...)
    pass through unchanged.

    Pre:    `inner_client` is a constructed Anthropic SDK client.
            `meter` is the RunMeter to accumulate into.
    Post:   `self.messages.create(**kwargs)` calls the inner client and
            records `usage` + duration into `meter`. All other
            attribute access proxies to the inner client.
    """

    def __init__(self, inner_client: Any, meter: RunMeter):
        self._inner = inner_client
        self.messages = _MeteredMessages(inner_client.messages, meter)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
