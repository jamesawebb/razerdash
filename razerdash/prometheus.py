"""Minimal Prometheus HTTP query client (stdlib only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class PrometheusError(Exception):
    pass


class Prometheus:
    def __init__(self, url: str, timeout: float = 3.0):
        self.base = url.rstrip("/")
        self.timeout = timeout

    def _get(self, endpoint: str, params: dict) -> dict:
        qs = urllib.parse.urlencode(params)
        url = f"{self.base}/api/v1/{endpoint}?{qs}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            raise PrometheusError(str(e)) from e
        if payload.get("status") != "success":
            raise PrometheusError(payload.get("error", "query failed"))
        return payload

    def query_range(self, promql: str, start: float, end: float,
                    step: float) -> list[tuple[float, float]]:
        """Run a range query, returning (timestamp, value) points of every
        series, merged and sorted by time."""
        payload = self._get("query_range", {"query": promql, "start": start,
                                            "end": end, "step": step})
        pts: list[tuple[float, float]] = []
        for s in payload.get("data", {}).get("result", []):
            for t, v in s.get("values", []):
                pts.append((float(t), float(v)))
        pts.sort(key=lambda p: p[0])
        return pts

    def query(self, promql: str) -> list[float]:
        """Run an instant query, returning the float value of every series."""
        payload = self._get("query", {"query": promql})

        data = payload.get("data", {})
        rt = data.get("resultType")
        result = data.get("result")
        vals: list[float] = []
        if rt == "scalar":
            vals.append(float(result[1]))
        elif rt == "vector":
            for s in result:
                vals.append(float(s["value"][1]))
        elif rt == "matrix":
            for s in result:
                if s.get("values"):
                    vals.append(float(s["values"][-1][1]))
        else:
            raise PrometheusError(f"unsupported resultType {rt!r}")
        return vals


def reduce_values(vals: list[float], mode: str):
    """Collapse a multi-series result to one scalar."""
    if not vals:
        return None
    if mode == "max":
        return max(vals)
    if mode == "min":
        return min(vals)
    if mode == "sum":
        return sum(vals)
    if mode == "avg":
        return sum(vals) / len(vals)
    if mode == "last":
        return vals[-1]
    return vals[0]  # "first" (default)
