"""HTTP access to public market data plus exchange clock validation.

No credentials are ever needed here: every endpoint is public read-only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests


class DataError(RuntimeError):
    pass


class PermanentDataError(DataError):
    """A 4xx that will never succeed on retry (unknown pair, bad parameter)."""


@dataclass
class HttpClient:
    proxy: str | None = None
    timeout: float = 15.0
    max_attempts: int = 3
    backoff: float = 1.5
    headers: dict[str, str] | None = None

    def _proxies(self) -> dict[str, str] | None:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.get(
                    url, params=params, proxies=self._proxies(), headers=self.headers, timeout=self.timeout
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise DataError(f"rate limited/server error {response.status_code} for {url}")
                if 400 <= response.status_code < 500:
                    raise PermanentDataError(f"client error {response.status_code} for {url}")
                response.raise_for_status()
                return response.json()
            except PermanentDataError:
                raise
            except (requests.RequestException, ValueError, DataError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(self.backoff * attempt)
        raise DataError(f"request failed after {self.max_attempts} attempts: {url} ({last_error})")

    def date_header_epoch(self, url: str) -> float | None:
        epoch, _round_trip = self.date_header_epoch_rtt(url)
        return epoch

    def date_header_epoch_rtt(self, url: str) -> tuple[float | None, float]:
        """Server clock from the HTTP ``Date`` header plus the measured round trip.

        Returns ``(None, rtt)`` instead of raising: the caller must fail closed (a
        clock we cannot check is a clock we may not trust), and 1-second header
        resolution is still a real measurement rather than an assumption. The round
        trip lets the caller take the network latency out of its skew figure.
        """
        started = time.monotonic()
        try:
            response = requests.get(
                url, proxies=self._proxies(), headers=self.headers, timeout=self.timeout, stream=True
            )
        except requests.RequestException:
            return None, time.monotonic() - started
        round_trip = time.monotonic() - started
        try:
            raw = response.headers.get("Date")
            if not raw:
                return None, round_trip
            return parsedate_to_datetime(raw).timestamp(), round_trip
        except (TypeError, ValueError):
            return None, round_trip
        finally:
            response.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(ts_seconds: float) -> datetime:
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc)


def check_clock_skew(client: HttpClient, server_time_url: str, key_path: tuple[str, ...], max_skew: float) -> dict[str, Any]:
    """Fail closed if the local clock cannot be trusted against the exchange.

    The exchange stamps its time before the response travels back, so the raw
    ``local - server`` figure also contains the network round trip: behind a slow
    proxy a healthy clock read as +2.2s and tripped the 2s gate (two PAPER cycles
    lost on 2026-10-26 while the true offset, checked over SSH against an NTP
    host, was ~+0.5s). The round trip is therefore measured and its half
    subtracted, NTP-style; the acceptable threshold itself is untouched.
    """
    started = time.monotonic()
    payload = client.get(server_time_url)
    round_trip = time.monotonic() - started
    cursor: Any = payload
    for part in key_path:
        if not isinstance(cursor, dict) or part not in cursor:
            raise DataError(f"unexpected server time payload: {payload}")
        cursor = cursor[part]
    server_ms = float(cursor)
    if server_ms > 1e12:
        server_ms /= 1000.0
    local = datetime.now(timezone.utc).timestamp()
    skew = local - server_ms - round_trip / 2.0
    return {
        "server_time": server_ms,
        "local_time": local,
        "round_trip_seconds": round(round_trip, 3),
        "skew_seconds": round(skew, 3),
        "acceptable": abs(skew) <= max_skew,
    }
