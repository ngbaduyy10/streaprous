"""TCP Reno congestion control for the TCP sender.

The sender may have at most cwnd bytes in flight. cwnd grows while ACKs arrive
and shrinks when a loss is detected. The three states and the events that move
between them follow the finite state machine of slide 3-125.
"""

# Slow start doubles cwnd each RTT (book 3.7.1). Avoidance adds about 1 MSS
# per RTT. Fast recovery starts after 3 duplicate ACKs.
SLOW_START = 'SLOW_START'
# About 1 MSS per RTT, the additive increase of AIMD (book 3.7.1, slide 3-120).
CONGESTION_AVOIDANCE = 'CONGESTION_AVOIDANCE'
# After 3 duplicate ACKs we halve and keep going (book 3.7.1).
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
        # Start at 1 MSS (slides 3-123 and 3-125).
        self.cwnd = mss
        # Where slow start ends. The first value is 64 KB on slide 3-125.
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
        # Half of cwnd, but never below 2 MSS (RFC 5681, not the book).
        half_window = self.cwnd // 2
        floor = 2 * self.mss
        self.ssthresh = max(half_window, floor)

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
            # One MSS per ACK, so the window doubles each round trip.
            one_segment = self.mss
            self.cwnd = self.cwnd + one_segment
            # The book says slow start ends when cwnd reaches ssthresh.
            # Slide 3-125 writes cwnd > ssthresh, so equal stays in slow start.
            passed_threshold = self.cwnd > self.ssthresh
            if passed_threshold:
                self._set_state(CONGESTION_AVOIDANCE)
        elif self.state == CONGESTION_AVOIDANCE:
            # About 1 MSS per round trip. At least 1 byte, so a huge window still grows.
            growth = self.mss * self.mss // self.cwnd
            if growth < 1:
                growth = 1
            self.cwnd = self.cwnd + growth
        else:
            # The lost segment arrived. Drop cwnd back to ssthresh.
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
            # One more segment has left the network, so there is room for one more.
            one_segment = self.mss
            self.cwnd = self.cwnd + one_segment
            return False
        self.dup_ack_count += 1
        third_duplicate = self.dup_ack_count == 3
        if third_duplicate:
            # Fast retransmit (book 3.5.4). The 3 MSS are already at the receiver (slide 3-125).
            self._halve_ssthresh()
            three_segments = 3 * self.mss
            self.cwnd = self.ssthresh + three_segments
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
        # Harder than 3 duplicate ACKs: nothing came back, so cwnd drops to 1 MSS (slides 3-121, 3-124).
        self._halve_ssthresh()
        one_segment = self.mss
        self.cwnd = one_segment
        self.dup_ack_count = 0
        self._set_state(SLOW_START)
