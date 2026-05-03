"""Entry point for the Mycelia symbiotic cognitive architecture simulation."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from .core.driver import OpenCLDriver
from .simulation.mycelia_world import MyceliaWorld
from .cognition.cognitive_core import CognitiveCore
from .visualization.renderer import WorldRenderer

LOGGER = logging.getLogger(__name__)


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def bootstrap_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
    )


def _candidate_driver_paths(path_config: Dict[str, Any]) -> Iterable[Path]:
    """Yield platform-appropriate driver paths in priority order."""

    defaults = [
        Path("./build/CC_OpenCl.dll"),
        Path("./build/libopencl_driver.dylib"),
        Path("./build/libopencl_driver.so"),
    ]

    if sys.platform == "win32":
        keys = ("driver_library_windows", "driver_library")
    elif sys.platform == "darwin":
        keys = ("driver_library_macos", "driver_library")
    else:
        keys = ("driver_library_linux", "driver_library")

    yielded = set()
    for key in keys:
        candidate = path_config.get(key)
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path not in yielded:
            yielded.add(path)
            yield path

    for path in defaults:
        expanded = path.expanduser()
        if expanded not in yielded:
            yielded.add(expanded)
            yield expanded


def _resolve_driver_library(path_config: Dict[str, Any]) -> Path:
    for candidate in _candidate_driver_paths(path_config):
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved

    search_list = ", ".join(str(p) for p in _candidate_driver_paths(path_config))
    raise FileNotFoundError(
        "Keine passende OpenCL-Bibliothek gefunden. Geprüfte Pfade: " + search_list
    )


def run_simulation(config_path: Path) -> None:
    bootstrap_logging()
    config = load_config(config_path)

    simulation_config = config["simulation"]
    quantum_config = dict(simulation_config.get("quantum", {}))
    quantum_config.setdefault("gpu_index", simulation_config.get("gpu_index", 0))

    if not quantum_config.get("enabled", True):
        os.environ.setdefault("CC_DISABLE_QUANTUM", "1")

    driver_path = _resolve_driver_library(config["paths"])
    LOGGER.info("Loading OpenCL driver from %s", driver_path)
    driver = OpenCLDriver(driver_path)
    if not quantum_config.get("enabled", True):
        driver.disable_quantum()

    world = MyceliaWorld(driver, simulation_config)
    cognition = CognitiveCore(driver, simulation_config["cognition"], quantum_config)
    renderer = WorldRenderer(driver, Path(config["paths"]["visualization_assets"]))

    LOGGER.info("Starting simulation loop")
    step = 0
    for snapshot in world.evolve():
        LOGGER.debug("Processing world snapshot at step %s", snapshot.step)
        cognition.reflect(snapshot)
        renderer.render(snapshot)

        step += 1
        if step >= simulation_config["max_steps"]:
            LOGGER.info("Reached maximum steps (%s). Ending simulation.", step)
            break

    LOGGER.info("Simulation complete")


if __name__ == "__main__":
    run_simulation(Path(__file__).with_name("config.yaml"))
