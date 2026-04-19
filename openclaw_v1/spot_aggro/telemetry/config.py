"""Env-driven telemetry config. SPOT AGGRO only. Never raises."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool
    questdb_host: str
    questdb_ilp_port: int
    questdb_pg_port: int
    queue_capacity: int
    worker_stop_timeout_s: float
    connect_timeout_s: float
    send_timeout_s: float
    sentry_backend_dsn: str
    sentry_frontend_dsn: str
    sentry_environment: str
    sentry_release: str
    sample_traces: float


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> TelemetryConfig:
    return TelemetryConfig(
        enabled=_env_bool("SPOT_TELEMETRY_ENABLED", True),
        questdb_host=_env("SPOT_QUESTDB_HOST", "127.0.0.1"),
        questdb_ilp_port=_env_int("SPOT_QUESTDB_ILP_PORT", 9009),
        questdb_pg_port=_env_int("SPOT_QUESTDB_PG_PORT", 8812),
        queue_capacity=_env_int("SPOT_TELEMETRY_QUEUE", 8192),
        worker_stop_timeout_s=_env_float("SPOT_TELEMETRY_STOP_TIMEOUT_S", 2.0),
        connect_timeout_s=_env_float("SPOT_TELEMETRY_CONNECT_TIMEOUT_S", 2.0),
        send_timeout_s=_env_float("SPOT_TELEMETRY_SEND_TIMEOUT_S", 1.5),
        sentry_backend_dsn=_env("SPOT_SENTRY_BACKEND_DSN", ""),
        sentry_frontend_dsn=_env("SPOT_SENTRY_FRONTEND_DSN", ""),
        sentry_environment=_env("SPOT_SENTRY_ENV", "production"),
        sentry_release=_env("SPOT_SENTRY_RELEASE", "dev"),
        sample_traces=_env_float("SPOT_SENTRY_TRACES", 0.05),
    )
