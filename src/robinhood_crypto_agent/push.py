"""One-off push notifications, the life-dashboard way.

A channel-agnostic POST to the ``STREAK_PUSH_WEBHOOK`` URL (an ntfy.sh topic, a
Slack/Discord webhook -- whatever the user points it at): the message is the
request body, the title an ntfy ``Title`` header. Best-effort by design: a
failure here is logged and swallowed so it can never stop the loop. Nothing
secret ever goes in a notification -- it is the same thin channel as the
dashboard, not the audit log.
"""

from __future__ import annotations

import logging

from . import net

log = logging.getLogger("rhca.push")

#: The webhook URL, shared with life-dashboard's streak-at-risk nudge.
WEBHOOK_ENV = "STREAK_PUSH_WEBHOOK"


def notify(webhook: str, title: str, message: str) -> bool:
    """POST ``message`` to ``webhook`` with ``title``. Returns whether it sent."""
    try:
        net.request("POST", webhook, headers={"Title": title}, body=message.encode("utf-8"))
        return True
    except net.HttpError as exc:
        log.warning("push notification failed: %s", exc)
        return False
