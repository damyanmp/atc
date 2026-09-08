"""Tests for the Prometheus query client (lib/prometheus.py).

Uses httpx.MockTransport (no network) to verify response parsing for the
instant query / range query / metric-name discovery paths.
"""
from __future__ import annotations

import httpx
import pytest

from config import PrometheusConfig
from lib.prometheus import list_metric_names, query, query_range


def _cfg() -> PrometheusConfig:
    return PrometheusConfig(host="prom.local", port=9090, https=False, verify_ssl=False)


_RealClient = httpx.Client


def _client_factory(handler):
    """Monkeypatch target: httpx.Client(...) -> Client wired to a mock transport."""
    def _make(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealClient(*args, **kwargs)
    return _make


class TestQuery:
    def test_instant_query_parses_results(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/query"
            return httpx.Response(200, json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"__name__": "up", "job": "prometheus"}, "value": [1700000000.0, "1"]},
                    ],
                },
            })
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        results = query(_cfg(), "up")
        assert len(results) == 1
        assert results[0].metric["job"] == "prometheus"
        assert results[0].value == 1.0

    def test_empty_result_returns_empty_list(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        assert query(_cfg(), "nonexistent_metric") == []

    def test_prometheus_error_status_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error", "error": "bad query"})
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        with pytest.raises(RuntimeError, match="bad query"):
            query(_cfg(), "not(valid")

    def test_http_error_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        with pytest.raises(RuntimeError, match="HTTP 500"):
            query(_cfg(), "up")


class TestQueryRange:
    def test_range_query_parses_values(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/query_range"
            return httpx.Response(200, json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "node_load1", "host": "docker-2"},
                            "values": [[1700000000.0, "0.5"], [1700000060.0, "0.7"]],
                        },
                    ],
                },
            })
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        results = query_range(_cfg(), "node_load1", window_minutes=10, step_seconds=60)
        assert len(results) == 1
        assert results[0].values == [(1700000000.0, 0.5), (1700000060.0, 0.7)]


class TestListMetricNames:
    def test_returns_sorted_names(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/label/__name__/values"
            return httpx.Response(200, json={"status": "success", "data": ["up", "node_load1", "container_cpu_usage_seconds_total"]})
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        names = list_metric_names(_cfg())
        assert names == ["container_cpu_usage_seconds_total", "node_load1", "up"]

    def test_filters_by_match_substring(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": ["up", "node_load1", "container_cpu_usage_seconds_total"]})
        monkeypatch.setattr("lib.prometheus.httpx.Client", _client_factory(handler))
        names = list_metric_names(_cfg(), match="cpu")
        assert names == ["container_cpu_usage_seconds_total"]
