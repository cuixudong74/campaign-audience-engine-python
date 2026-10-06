"""Background scheduler that activates scheduled campaigns.

The scheduler is a thin timer over :meth:`CampaignService.activate_due`.
All correctness lives in that idempotent database operation, so starting
and stopping the scheduler (including a service restart) never double
activates or corrupts a campaign: on boot, simply call ``activate_due``
once for campaigns whose scheduled time passed while the service was
down.
"""
from __future__ import annotations

import threading

from .lifecycle import CampaignService


class ActivationScheduler:
    def __init__(self, campaigns: CampaignService, interval_seconds: float = 0.05):
        self.campaigns = campaigns
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # catch up immediately, then tick in background
        self.catch_up()
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="activation-scheduler", daemon=True)
            self._thread.start()

    def catch_up(self) -> list[int]:
        """Activate everything already due (safe to call on startup)."""
        return self.campaigns.activate_due()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.campaigns.activate_due()
            except Exception:
                # a transient DB error must not kill the ticker
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
