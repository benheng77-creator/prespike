"""
Sentry bridge — dormant stub until SPOT_SENTRY_BACKEND_DSN is set.

When the DSN is empty OR sentry_sdk is not installed, all functions here
are cheap no-ops. The emitter can call into this module unconditionally.

Mandatory tag on every event: engine=spot_aggro (isolation from any
future apex_omega Sentry project).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("spot_aggro.telemetry.sentry")

try:
    import sentry_sdk  # type: ignore
    _SDK_AVAILABLE = True
except ImportError:
    sentry_sdk = None  # type: ignore
    _SDK_AVAILABLE = False

_ACTIVE = False


def init_sentry(cfg) -> None:
    """Initialise Sentry if DSN is present and SDK is available.

    Never raises. If anything goes wrong, logs a warning and leaves the
    bridge dormant.
    """
    global _ACTIVE
    _ACTIVE = False
    if not _SDK_AVAILABLE:
        log.info("sentry_sdk not installed — Sentry dormant")
        return
    if not getattr(cfg, "sentry_backend_dsn", ""):
        log.info("SPOT_SENTRY_BACKEND_DSN unset — Sentry dormant")
        return
    try:
        integrations = []
        # Best-effort integrations; SDK versions vary.
        try:
            from sentry_sdk.integrations.fastapi import FastApiIntegration  # type: ignore
            integrations.append(FastApiIntegration())
        except Exception:  # noqa: BLE001
            pass
        try:
            from sentry_sdk.integrations.asyncio import AsyncioIntegration  # type: ignore
            integrations.append(AsyncioIntegration())
        except Exception:  # noqa: BLE001
            pass
        try:
            from sentry_sdk.integrations.logging import LoggingIntegration  # type: ignore
            integrations.append(LoggingIntegration(
                level=logging.INFO, event_level=logging.ERROR
            ))
        except Exception:  # noqa: BLE001
            pass

        sentry_sdk.init(
            dsn=cfg.sentry_backend_dsn,
            environment=cfg.sentry_environment,
            release=cfg.sentry_release,
            traces_sample_rate=cfg.sample_traces,
            integrations=integrations,
            send_default_pii=False,
        )
        sentry_sdk.set_tag("engine", "spot_aggro")
        sentry_sdk.set_tag("component", "backend")
        _ACTIVE = True
        log.info("Sentry active: env=%s release=%s traces=%.3f",
                 cfg.sentry_environment, cfg.sentry_release, cfg.sample_traces)
    except Exception as exc:  # noqa: BLE001
        log.warning("Sentry init failed: %s — staying dormant", exc)
        _ACTIVE = False


def active() -> bool:
    return _ACTIVE


def _apply_tags(scope, tags: Optional[dict]) -> None:
    scope.set_tag("engine", "spot_aggro")
    if tags:
        for k, v in tags.items():
            try:
                scope.set_tag(k, str(v))
            except Exception:  # noqa: BLE001
                pass


def capture_message(msg: str, *, level: str = "info",
                    tags: Optional[dict] = None) -> None:
    if not _ACTIVE or sentry_sdk is None:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            _apply_tags(scope, tags)
            sentry_sdk.capture_message(msg, level=level)
    except Exception:  # noqa: BLE001
        pass


def capture_exception(exc: BaseException, *,
                      tags: Optional[dict] = None) -> None:
    if not _ACTIVE or sentry_sdk is None:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            _apply_tags(scope, tags)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass
