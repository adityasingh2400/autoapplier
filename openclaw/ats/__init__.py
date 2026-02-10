from __future__ import annotations

from openclaw.utils import ATSKind, detect_ats

from .ashby import AshbyHandler
from .base import BaseATSHandler
from .generic import GenericATSHandler
from .greenhouse import GreenhouseHandler
from .lever import LeverHandler
from .workday import WorkdayHandler


def handler_for_kind(kind: ATSKind) -> BaseATSHandler:
    if kind == ATSKind.GREENHOUSE:
        return GreenhouseHandler()
    if kind == ATSKind.LEVER:
        return LeverHandler()
    if kind == ATSKind.ASHBY:
        return AshbyHandler()
    if kind == ATSKind.WORKDAY:
        return WorkdayHandler()
    return GenericATSHandler()


def handler_for_url(url: str) -> BaseATSHandler:
    return handler_for_kind(detect_ats(url))


__all__ = [
    "BaseATSHandler",
    "GenericATSHandler",
    "GreenhouseHandler",
    "LeverHandler",
    "AshbyHandler",
    "WorkdayHandler",
    "handler_for_kind",
    "handler_for_url",
]
