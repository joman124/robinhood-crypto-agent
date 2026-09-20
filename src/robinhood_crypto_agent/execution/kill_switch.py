from __future__ import annotations

from pathlib import Path

DEFAULT_KILL_SWITCH_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "kill_switch.flag"
)


def is_kill_switch_engaged(path: Path = DEFAULT_KILL_SWITCH_PATH) -> bool:
    return path.exists()


def engage_kill_switch(path: Path = DEFAULT_KILL_SWITCH_PATH, reason: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((reason or "kill switch manually engaged") + "\n")


def disengage_kill_switch(path: Path = DEFAULT_KILL_SWITCH_PATH) -> None:
    if path.exists():
        path.unlink()
