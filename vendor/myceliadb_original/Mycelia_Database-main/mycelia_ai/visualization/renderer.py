"""Rendering facade for visualizing the evolving Mycelia world."""
from __future__ import annotations

import logging
from pathlib import Path

from ..core.driver import OpenCLDriver
from ..simulation.mycelia_world import WorldSnapshot

LOGGER = logging.getLogger(__name__)


class WorldRenderer:
    """Handles presentation of the simulation state."""

    def __init__(self, driver: OpenCLDriver, asset_path: Path) -> None:
        self._driver = driver
        self._asset_path = asset_path

    def render(self, snapshot: WorldSnapshot) -> None:
        LOGGER.debug(
            "Rendering snapshot %s with assets at %s", snapshot.step, self._asset_path
        )
        if self._driver.context_ready:
            LOGGER.debug(
                "GPU-Renderer noch nicht implementiert – verwende symbolische Ausgabe."
            )
