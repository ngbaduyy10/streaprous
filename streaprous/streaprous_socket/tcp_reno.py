"""
StreapRous TCP Reno
Keeps cwnd and ssthresh with the three states of the finite state machine on slide 3-125.
"""

from .tcp_trace import no_log

# Slow start doubles cwnd each RTT (book 3.7.1, slide 3-123).
SLOW_START = 'SLOW_START'
# About 1 MSS per RTT, the additive increase of AIMD (book 3.7.1, slide 3-120).
CONGESTION_AVOIDANCE = 'CONGESTION_AVOIDANCE'
# After 3 duplicate ACKs we halve cwnd and keep going (book 3.7.1, slide 3-121).
FAST_RECOVERY = 'FAST_RECOVERY'
# Where slow start ends: 65536 bytes, about 45 MSS at MSS 1460. Below it cwnd
# grows in slow start; above it, in congestion avoidance (book 3.7.1).
INITIAL_SSTHRESH = 64 * 1024        # slide 3-125: ssthresh = 64 KB


class RenoCongestion:
    """Holds cwnd, ssthresh, dupACKcount and the state of slide 3-125.

    Attributes:
         cwnd           congestion window in bytes
         ssthresh       slow start threshold in bytes
         dup_ack_count  duplicate ACKs counted (dupACKcount)
         state          SLOW_START, CONGESTION_AVOIDANCE or FAST_RECOVERY
    """

    def __init__(self, mss, ssthresh=INITIAL_SSTHRESH, log=no_log):
        """Starts in slow start, the start arrow of slide 3-125.

        cwnd = 1 MSS, ssthresh = 64 KB, dupACKcount = 0.

        Args:
             mss       maximum segment size in bytes
             ssthresh  first ssthresh in bytes (64 KB)
             log       called as log('STATE', old=..., new=...) at each state change
        """
        self.mss = mss
        self.cwnd = mss
        self.ssthresh = ssthresh
        self.dup_ack_count = 0
        self.state = SLOW_START
        self.log = log

    def on_new_ack(self):
        """Handles an ACK for new data, the "new ACK" arrows of slide 3-125.

        Slow start: cwnd = cwnd+MSS (slide 3-123: double cwnd every RTT, done by
        incrementing cwnd for every ACK received), then the cwnd > ssthresh arrow.
        Congestion avoidance: cwnd = cwnd + MSS*(MSS/cwnd), about one MSS per RTT.
        Fast recovery: cwnd = ssthresh and go to congestion avoidance.
        In every state dupACKcount = 0.

        extra: MSS*(MSS/cwnd) is rounded down to whole bytes (round down), because
        windows are whole bytes.
        """
        self.dup_ack_count = 0
        if self.state == SLOW_START:
            # +1 MSS per ACK, so the window doubles each RTT.
            self.cwnd = self.cwnd + self.mss
            self.check_slow_start_end()
        elif self.state == CONGESTION_AVOIDANCE:
            # MSS*MSS/cwnd per ACK is about 1 MSS per RTT.
            self.cwnd = self.cwnd + self.mss * self.mss // self.cwnd
        else:
            # The lost segment arrived. Drop cwnd back to ssthresh.
            self.cwnd = self.ssthresh
            self.enter(CONGESTION_AVOIDANCE)

    def on_duplicate_ack(self):
        """Handles a duplicate ACK, the "duplicate ACK" arrows of slide 3-125.

        Slow start or congestion avoidance: dupACKcount++. When dupACKcount == 3:
        ssthresh = cwnd/2, cwnd = ssthresh + 3 MSS, retransmit missing segment and
        go to fast recovery. Fast recovery: cwnd = cwnd + MSS. cwnd/2 rounds down.

        Returns:
             True exactly when the caller must retransmit the missing segment now
        """
        if self.state == FAST_RECOVERY:
            # One more segment has left the network, so there is room for one more.
            self.cwnd = self.cwnd + self.mss
            return False
        self.dup_ack_count = self.dup_ack_count + 1
        if self.dup_ack_count == 3:
            # Fast retransmit (book 3.5.4). The 3 MSS are already at the receiver (slide 3-125).
            self.ssthresh = self.cwnd // 2
            self.cwnd = self.ssthresh + 3 * self.mss
            self.enter(FAST_RECOVERY)
            return True
        return False

    def on_timeout(self):
        """Handles a timeout in any state, the "timeout" arrows of slide 3-125.

        slide 3-124: on loss event, ssthresh is set to 1/2 of cwnd just before loss
        event (rounded down). slide 3-125: cwnd = 1 MSS, dupACKcount = 0, go to
        slow start. If cwnd is already above ssthresh, the cwnd > ssthresh arrow
        leads on to congestion avoidance at once.
        """
        # Worse than 3 duplicate ACKs: nothing came back, so cwnd drops to 1 MSS
        # (slides 3-121, 3-124).
        self.ssthresh = self.cwnd // 2
        self.cwnd = self.mss
        self.dup_ack_count = 0
        self.enter(SLOW_START)
        self.check_slow_start_end()

    def check_slow_start_end(self):
        """Follows the arrow out of slow start (slide 3-125: cwnd > ssthresh).

        The arrow needs no event: in slow start with cwnd > ssthresh, Reno goes to
        congestion avoidance.
        """
        # The book says slow start ends when cwnd reaches ssthresh. Slide 3-125
        # writes cwnd > ssthresh, so equal stays in slow start.
        if self.state == SLOW_START and self.cwnd > self.ssthresh:
            self.enter(CONGESTION_AVOIDANCE)

    def enter(self, state):
        """Moves to a state and logs STATE when the state really changes.

        Args:
             state  SLOW_START, CONGESTION_AVOIDANCE or FAST_RECOVERY
        """
        old = self.state
        self.state = state
        if old != state:
            self.log('STATE', old=old, new=state)
