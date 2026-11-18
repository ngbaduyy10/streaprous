"""
StreapRous TCP round trip time
Keeps EstimatedRTT, DevRTT and TimeoutInterval for the sender's timer (slides 3-81 to 3-83).
"""

ALPHA = 0.125                      # slide 3-82
BETA = 0.25                        # slide 3-83
FIRST_TIMEOUT_INTERVAL = 1.0       # extra: before the first SampleRTT
MIN_TIMEOUT_INTERVAL = 0.2         # extra: never below 0.2 s


class RttEstimator:
    """Turns SampleRTT values into the TimeoutInterval of the sender's timer.

    Attributes:
         estimated_rtt     EWMA of SampleRTT in seconds, None until the first sample
         dev_rtt           EWMA of how far SampleRTT is from EstimatedRTT, None until then
         timeout_interval  how long the timer waits, in seconds
    """

    def __init__(self, first_timeout_interval=FIRST_TIMEOUT_INTERVAL,
                 min_timeout_interval=MIN_TIMEOUT_INTERVAL):
        """Starts with no SampleRTT yet.

        extra: TimeoutInterval is 1 s before the first SampleRTT. The timer needs
        a value before any ACK came back, and the slides give none.

        Args:
             first_timeout_interval  TimeoutInterval before the first sample (1 s)
             min_timeout_interval    the lowest TimeoutInterval allowed (0.2 s)
        """
        self.min_timeout_interval = min_timeout_interval
        self.estimated_rtt = None
        self.dev_rtt = None
        self.timeout_interval = first_timeout_interval

    def add_sample(self, sample_rtt):
        """Takes one SampleRTT and updates the estimates.

        The slides do not fix the order; we follow their page order. A later sample
        runs slide 3-82 (EstimatedRTT) first, then slide 3-83 (DevRTT), then
        TimeoutInterval.

        extra: the first SampleRTT starts both averages: EstimatedRTT = SampleRTT
        and DevRTT = SampleRTT/2, because each EWMA needs an old value.

        Args:
             sample_rtt  measured round trip time in seconds
        """
        if self.estimated_rtt is None:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.update_estimated_rtt(sample_rtt)
            self.update_dev_rtt(sample_rtt)
        self.update_timeout_interval()

    def update_estimated_rtt(self, sample_rtt):
        """Moves EstimatedRTT towards the new sample (slide 3-82).

        EstimatedRTT = (1 - alpha)*EstimatedRTT + alpha*SampleRTT, alpha = 0.125.

        Args:
             sample_rtt  measured round trip time in seconds
        """
        self.estimated_rtt = (1 - ALPHA) * self.estimated_rtt + ALPHA * sample_rtt

    def update_dev_rtt(self, sample_rtt):
        """Moves DevRTT towards the new deviation (slide 3-83).

        DevRTT = (1 - beta)*DevRTT + beta*|SampleRTT - EstimatedRTT|, beta = 0.25.
        The slides do not say whether this uses the old or the new EstimatedRTT; we
        use the new one, already updated by slide 3-82 (RFC 6298 uses the old one,
        background).

        Args:
             sample_rtt  measured round trip time in seconds
        """
        deviation = abs(sample_rtt - self.estimated_rtt)
        self.dev_rtt = (1 - BETA) * self.dev_rtt + BETA * deviation

    def update_timeout_interval(self):
        """Sets the timer's interval from the two averages (slide 3-83).

        TimeoutInterval = EstimatedRTT + 4*DevRTT: the estimate plus a safety margin.

        extra: never below 0.2 s. Linux clients delay an ACK at most 200 ms and our
        receiver 100 ms (Linux, background), and that delay already feeds SampleRTT,
        so a shorter timer would resend segments that were not lost.
        """
        timeout = self.estimated_rtt + 4 * self.dev_rtt
        self.timeout_interval = max(self.min_timeout_interval, timeout)
