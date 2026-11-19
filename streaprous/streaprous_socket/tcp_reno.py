"""TCP Reno congestion control for the TCP sender.

The sender may have at most cwnd bytes in flight. cwnd grows while ACKs arrive
and shrinks when a loss is detected. The three states and the events that move
between them follow the finite state machine of slide 3-125.
"""

# WHY RENO: the book (3.7.1) describes classic TCP congestion control as
# three parts: slow start, congestion avoidance and fast recovery. The
# slides (3-120 to 3-125) draw this as one state machine, so this class is
# that machine: one method per event (new ACK, duplicate ACK, timeout).
# The book calls fast recovery recommended but not required. Reno has it,
# Tahoe does not (Tahoe drops to 1 MSS after a triple duplicate ACK too).
# The class knows nothing about sockets or time, so every row of the state
# machine can be unit tested without a network.

# NAME = 'NAME' creates a constant. The quoted text is only the name of
# the state, not a number. The state says how cwnd grows the next time an
# ACK arrives (slide 3-125).
# Slow start (book 3.7.1): cwnd starts at 1 MSS and grows by 1 MSS per ACK,
# which doubles cwnd every RTT. WHY start small: the sender does not know
# the capacity of the path, so it probes quickly but cautiously.
# Slow start: cwnd grows by 1 MSS per ACK, so it roughly doubles every round.
# "Slow" means we start at 1 MSS, not that the growth is slow.
SLOW_START = 'SLOW_START'
# Congestion avoidance (book 3.7.1): cwnd is now about half of where
# congestion was last seen, so it grows linearly, about 1 MSS per RTT.
# WHY: we are probably close to the limit, so we probe gently instead of
# doubling into a loss (additive increase of AIMD, book 3.7.1 and 3.7.3).
# Congestion avoidance: careful growth, about 1 MSS per round trip (AIMD, slide 3-120).
CONGESTION_AVOIDANCE = 'CONGESTION_AVOIDANCE'
# Fast recovery (book 3.7.1): entered after 3 duplicate ACKs. Duplicate ACKs
# mean the network still delivers segments, so we halve and keep going
# instead of restarting from 1 MSS (multiplicative decrease of AIMD).
# Fast recovery: after three identical ACKs. We halve and continue, we do not start over.
FAST_RECOVERY = 'FAST_RECOVERY'


# class is the recipe. Each TCP connection makes its own RenoCongestion.
# self inside the methods is that object. def is a function.
# on_event=None means the argument can be left out; then the value is None.
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
        # self.field = value stores the value on the object. MSS is the
        # largest amount of data in one segment. cwnd grows in whole MSS.
        self.mss = mss
        # cwnd is counted in bytes: the book (3.7.1) limits the unACKed data
        # to min(cwnd, rwnd) bytes. cwnd protects the network, rwnd
        # (flow control, 3.5.5) protects the receiver's buffer.
        # Start value 1 MSS: slides 3-123 and 3-125.
        # cwnd is the congestion window: how many bytes we may have out
        # without an acknowledgement. We start at 1 MSS (slide 3-123) and
        # grow only when ACKs arrive.
        self.cwnd = mss
        # ssthresh: the cwnd value where slow start ends and the careful
        # growth of congestion avoidance starts (book 3.7.1). The initial
        # value is 64 KB (65536 bytes) on slide 3-125; it can be changed
        # with STREAPROUS_SSTHRESH.
        self.ssthresh = initial_ssthresh
        # Book 3.5.4: a duplicate ACK re-acknowledges data that was already
        # acknowledged. Three in a row (four ACKs for the same data) is read
        # as a lost segment. WHY three: one or two can come from simple
        # reordering in the network, three is a stronger sign of loss.
        # = 0 resets the counter. Three identical ACKs in a row are read as the loss of one segment.
        self.dup_ack_count = 0
        # Store the starting state. SLOW_START is the constant above, the text name.
        self.state = SLOW_START
        # Store the optional logger. It is called only when the state actually
        # changes, so the graph gets one point per change.
        self.on_event = on_event

    def _set_state(self, new_state):
        """Moves to new_state and reports the change when the state really changes.

        Args:
             new_state  the state to enter
        """
        # Remember the name we have now, before the line below overwrites it.
        old_state = self.state
        self.state = new_state
        # != means "not equal". and requires both parts to be true.
        # is not None means a function was actually passed in.
        # Report only a real change, so the same state does not add a log line.
        if old_state != new_state and self.on_event is not None:
            # Call the function. old= and new= are named arguments: they set
            # the parameters old and new, whatever the order.
            self.on_event('STATE', old=old_state, new=new_state)

    def _halve_ssthresh(self):
        """Sets ssthresh to half of cwnd, but never below 2 MSS.

        The floor of 2 MSS comes from RFC 5681, not from the slides. Without
        it a tiny cwnd would give a tiny ssthresh and slow start could not grow.
        """
        # Multiplicative decrease (book 3.7.1, AIMD in 3.7.3): after a loss
        # event ssthresh becomes half of cwnd (slide 3-125 uses cwnd/2).
        # The floor of 2 MSS is NOT in the book or the slides. It comes from
        # RFC 5681, which also uses FlightSize where the slides use cwnd; in
        # a fully used window they are about equal.
        # max(a, b) picks the larger number. // is integer division: 9111 // 2
        # becomes 4555, the fraction is dropped. 2 * self.mss is the floor of 2 MSS.
        # Multiplicative decrease in AIMD: remember about half of where the
        # network started to fill up. Without the floor (RFC 5681) a small cwnd
        # could give an ssthresh so low that slow start never got to grow.
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
        # = 0 resets the counter. A new ACK acknowledges new bytes and breaks
        # the run of identical ACKs, in all three states.
        self.dup_ack_count = 0
        # Book 3.7.1: every new ACK is a sign that the network delivers, so
        # cwnd may grow. How fast depends on the state: exponential in slow
        # start, linear in congestion avoidance, and a deflate to ssthresh
        # when the ACK ends fast recovery. ACKs set the pace: TCP is
        # self-clocking (3.7.1), a new ACK releases new segments.
        # if / elif / else: exactly one branch runs. == compares two values,
        # it does not store. Here: is the state name the text SLOW_START?
        if self.state == SLOW_START:
            # += is short for self.cwnd = self.cwnd + self.mss: add one MSS.
            # Each ACK frees room for two new segments, so the window roughly
            # doubles every round trip.
            self.cwnd += self.mss
            # The book text says slow start ends when cwnd reaches ssthresh;
            # slide 3-125 writes cwnd > ssthresh. I follow the slide. The
            # difference is one ACK, and RFC 5681 allows both.
            # > means "greater than". Equal to ssthresh stays in slow start
            # (slide 3-123). _set_state(...) calls the function and changes the name.
            if self.cwnd > self.ssthresh:
                self._set_state(CONGESTION_AVOIDANCE)
        elif self.state == CONGESTION_AVOIDANCE:
            # Book 3.7.1: cwnd += MSS * (MSS / cwnd) per ACK. A window of
            # cwnd / MSS segments gives cwnd / MSS ACKs per RTT, so the sum is
            # about 1 MSS per RTT. Integer division can give 0 when cwnd is
            # very large, so max(1, ...) keeps the window growing.
            # * multiplies, // divides with integers afterwards. max(1, ...)
            # takes at least 1, so the window never stalls. MSS*MSS/cwnd per
            # ACK adds up to about 1 MSS per round: the additive increase of
            # AIMD (slide 3-120).
            self.cwnd += max(1, self.mss * self.mss // self.cwnd)
        else:
            # Book 3.7.1: when the ACK for the missing segment arrives, TCP
            # deflates cwnd and enters congestion avoidance. The extra MSS
            # added by duplicate ACKs during recovery are removed here.
            # else runs when no if/elif matched. Here that is fast recovery.
            # = copies ssthresh into cwnd ("deflate"): the lost segment
            # arrived, and we continue carefully in congestion avoidance.
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
        # Book 3.7.1: in fast recovery cwnd grows by 1 MSS for every
        # duplicate ACK. Each one means another segment left the network and
        # sits at the receiver, so there is room for one new segment.
        if self.state == FAST_RECOVERY:
            # += self.mss is cwnd = cwnd + mss. One more segment has left the
            # network and sits at the receiver ("inflate"), so we can send
            # new data while we wait for the lost one.
            self.cwnd += self.mss
            # return stops the function at once and hands the value back to
            # the caller. False: we already retransmitted on the third one.
            return False
        # += 1 increases the counter by one.
        self.dup_ack_count += 1
        # Fast retransmit (book 3.5.4): because a sender sends many segments
        # back to back, one lost segment gives several duplicate ACKs. At the
        # third one we resend without waiting for the timer. This also
        # starts fast recovery: ssthresh = cwnd/2 and cwnd = ssthresh + 3 MSS
        # (slide 3-125). The 3 MSS stand for the three segments that caused
        # the duplicate ACKs. They are at the receiver, not in the network.
        # == 3 tests whether the counter is exactly 3, not "at least 3". One
        # or two identical ACKs can just be reordering. The third is the sign
        # that one segment is missing while the network otherwise works
        # (slides 3-88 and 3-125).
        if self.dup_ack_count == 3:
            # Call the helper. It returns nothing; it writes ssthresh.
            self._halve_ssthresh()
            # + adds, * multiplies first. Three segments are already at the
            # receiver and no longer take space in the network, so the window
            # is filled with 3 MSS.
            self.cwnd = self.ssthresh + 3 * self.mss
            self._set_state(FAST_RECOVERY)
            # return True stops here and asks the sender to resend the lost one now.
            return True
        # Not the third one: return False. The count is done, no retransmit.
        return False

    def on_timeout(self):
        """Handles a retransmission timeout in any state (slides 3-124, 3-125).

        A timeout is a worse sign than duplicate ACKs, so the sender starts
        over: ssthresh becomes half of cwnd (at least 2 MSS, RFC 5681), cwnd
        goes back to one MSS, the duplicate ACK counter restarts and the state
        is slow start.
        """
        # Book 3.7.1: a timeout in any state gives ssthresh = cwnd/2,
        # cwnd = 1 MSS and slow start. WHY harder than 3 duplicate ACKs:
        # with duplicate ACKs segments still get through, after a timeout
        # nothing came back, so the network is probably badly congested.
        # This is the Tahoe rule on slide 3-121, and Reno keeps it.
        # Call without using the return value. The function halves ssthresh
        # and returns None. A timeout is worse than three dupACKs: nothing
        # came back, so the network is probably congested (slide 3-124).
        self._halve_ssthresh()
        # = copies 1 MSS into cwnd. Drop all the way back and start slow start
        # again, instead of continuing at half speed the way fast recovery does.
        self.cwnd = self.mss
        # = 0 resets the counter of identical ACKs.
        self.dup_ack_count = 0
        self._set_state(SLOW_START)
