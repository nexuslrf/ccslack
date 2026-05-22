"""Status polling — periodically reconciles channel/window state with tmux.

Walking-skeleton scope (drives only what the Slack UX needs to look alive):

  * **Dead-window detection.** A bound window whose tmux entry has vanished is
    flipped to ``"dead"`` and a recovery banner is posted to the bound channel.
  * **Active→idle decay.** When the cached ``status_state`` is ``"active"`` and
    no transcript message has arrived in ``IDLE_DECAY_SECONDS``, the status is
    flipped back to ``"idle"``.

Active and Stop transitions are driven elsewhere — by ``message_routing``
(sets active) and ``hook_events`` (sets done). The polling loop only fills the
gap when nothing else fires.
"""

from .coordinator import start_status_polling, stop_status_polling

__all__ = ["start_status_polling", "stop_status_polling"]
