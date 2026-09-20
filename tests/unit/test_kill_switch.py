from __future__ import annotations

from robinhood_crypto_agent.execution.kill_switch import (
    disengage_kill_switch,
    engage_kill_switch,
    is_kill_switch_engaged,
)


def test_engage_and_disengage(tmp_path):
    path = tmp_path / "kill_switch.flag"
    assert not is_kill_switch_engaged(path)

    engage_kill_switch(path, reason="test")
    assert is_kill_switch_engaged(path)
    assert "test" in path.read_text()

    disengage_kill_switch(path)
    assert not is_kill_switch_engaged(path)


def test_disengage_when_not_engaged_is_a_no_op(tmp_path):
    path = tmp_path / "kill_switch.flag"
    disengage_kill_switch(path)  # should not raise
    assert not is_kill_switch_engaged(path)


def test_engage_without_reason_still_writes_a_file(tmp_path):
    path = tmp_path / "kill_switch.flag"
    engage_kill_switch(path)
    assert is_kill_switch_engaged(path)
    assert path.read_text().strip() != ""
