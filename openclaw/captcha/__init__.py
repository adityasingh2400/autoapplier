"""CAPTCHA detection and optional solving support."""

from .detector import CaptchaDetection, detect_captcha
from .solver import can_auto_solve, try_solve_captcha

__all__ = ["CaptchaDetection", "detect_captcha", "can_auto_solve", "try_solve_captcha"]
