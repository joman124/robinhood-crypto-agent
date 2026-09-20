"""The kill switch fails safe: unknown state blocks."""

from robinhood_crypto_agent.execution.kill_switch import (
    REASON_DAILY_LOSS,
    REASON_MANUAL,
    KillSwitch,
)


def test_absent_file_means_released(tmp_path):
    assert not KillSwitch(tmp_path / "KILL_SWITCH").engaged


def test_engage_and_release(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    state = switch.engage(REASON_MANUAL)
    assert state.engaged and state.engaged_at is not None
    assert "ENGAGED" in state.describe()
    assert not switch.release().engaged


def test_re_engaging_keeps_the_original_reason(tmp_path):
    """The first thing that tripped the switch is the interesting one."""
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    switch.engage(REASON_DAILY_LOSS)
    switch.engage(REASON_MANUAL)
    assert switch.state().reason == REASON_DAILY_LOSS


def test_releasing_a_released_switch_is_a_noop(tmp_path):
    assert not KillSwitch(tmp_path / "KILL_SWITCH").release().engaged


def test_an_unreadable_switch_reads_as_engaged(tmp_path):
    """Unknown state must block, never permit."""
    path = tmp_path / "KILL_SWITCH"
    path.write_text("{corrupt")
    state = KillSwitch(path).state()
    assert state.engaged
    assert "unreadable" in state.reason


def test_a_switch_engaged_externally_is_honoured(tmp_path):
    """`touch data/KILL_SWITCH` from any shell must stop trading."""
    path = tmp_path / "KILL_SWITCH"
    path.write_text("")
    assert KillSwitch(path).engaged
