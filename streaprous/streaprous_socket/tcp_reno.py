"""TCP Reno congestion control for the TCP sender.

The sender may have at most cwnd bytes in flight. cwnd grows while ACKs arrive
and shrinks when a loss is detected. The three states and the events that move
between them follow the finite state machine of slide 3-125.
"""

SLOW_START = 'SLOW_START'
CONGESTION_AVOIDANCE = 'CONGESTION_AVOIDANCE'
FAST_RECOVERY = 'FAST_RECOVERY'


class RenoCongestion:
    """Holds cwnd, ssthresh and the state of the Reno machine (slide 3-125).

    Attributes:
         cwnd            congestion window in bytes, starts at one MSS
         ssthresh        slow start threshold in bytes
         dup_ack_count   duplicate ACKs seen in a row
         state           SLOW_START, CONGESTION_AVOIDANCE or FAST_RECOVERY
    """

    def __init__(self, mss, initial_ssthresh, on_event=None):
        """Starts in slow start with a window of one MSS.

        Args:
             mss               maximum segment size in bytes
             initial_ssthresh  first value of ssthresh in bytes
             on_event=None     called as on_event('STATE', old=..., new=...) at
                               every state change; None means no events
        """
        self.mss = mss
        self.cwnd = mss
        self.ssthresh = initial_ssthresh
        self.dup_ack_count = 0
        self.state = SLOW_START
        self.on_event = on_event

    def _set_state(self, new_state):
        """Moves to new_state and reports the change when the state really changes.

        Args:
             new_state  the state to enter
        """
        old_state = self.state
        self.state = new_state
        if old_state != new_state and self.on_event is not None:
            self.on_event('STATE', old=old_state, new=new_state)

    def _halve_ssthresh(self):
        """Sets ssthresh to half of cwnd, but never below 2 MSS.

        The floor of 2 MSS comes from RFC 5681, not from the slides. Without
        it a tiny cwnd would give a tiny ssthresh and slow start could not grow.
        """
        self.ssthresh = max(self.cwnd // 2, 2 * self.mss)

    def on_new_ack(self):
        """Handles an ACK that acknowledges new data (slides 3-123, 3-125).

        In slow start cwnd grows by one MSS per ACK, so it doubles every round
        trip. Only when cwnd goes above ssthresh the sender switches to
        congestion avoidance (cwnd equal to ssthresh stays in slow start). In
        congestion avoidance cwnd grows by about MSS * MSS / cwnd per ACK, which
        is one MSS per round trip, and by at least 1 byte so the window never
        stalls. A new ACK in fast recovery means the lost segment arrived: cwnd
        drops to ssthresh and congestion avoidance continues. In every state the
        duplicate ACK counter restarts from 0.
        """
        self.dup_ack_count = 0
        if self.state == SLOW_START:
            self.cwnd += self.mss
            if self.cwnd > self.ssthresh:
                self._set_state(CONGESTION_AVOIDANCE)
        elif self.state == CONGESTION_AVOIDANCE:
            self.cwnd += max(1, self.mss * self.mss // self.cwnd)
        else:
            self.cwnd = self.ssthresh
            self._set_state(CONGESTION_AVOIDANCE)

    def on_duplicate_ack(self):
        """Handles an ACK that repeats the previous ACK number (slide 3-125).

        Duplicate ACKs in slow start or congestion avoidance are only counted.
        The third one is read as a loss: ssthresh becomes half of cwnd (at
        least 2 MSS, RFC 5681), cwnd becomes ssthresh + 3 MSS for the three
        segments that already left the network, and the state becomes fast
        recovery. In fast recovery each further duplicate ACK means another
        segment left the network, so cwnd grows by one MSS.

        Returns:
             True exactly when the caller must fast retransmit now (the third
             duplicate ACK), otherwise False
        """
        if self.state == FAST_RECOVERY:
            self.cwnd += self.mss
            return False
        self.dup_ack_count += 1
        if self.dup_ack_count == 3:
            self._halve_ssthresh()
            self.cwnd = self.ssthresh + 3 * self.mss
            self._set_state(FAST_RECOVERY)
            return True
        return False

    def on_timeout(self):
        """Handles a retransmission timeout in any state (slides 3-124, 3-125).

        A timeout is a worse sign than duplicate ACKs, so the sender starts
        over: ssthresh becomes half of cwnd (at least 2 MSS, RFC 5681), cwnd
        goes back to one MSS, the duplicate ACK counter restarts and the state
        is slow start.
        """
        self._halve_ssthresh()
        self.cwnd = self.mss
        self.dup_ack_count = 0
        self._set_state(SLOW_START)
