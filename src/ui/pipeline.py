"""Compatibility import; task orchestration lives outside the UI package."""
from ..services.scheduler import Scheduler

Pipeline = Scheduler
