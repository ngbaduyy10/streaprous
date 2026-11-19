"""Timeout estimator for the TCP sender.

The sender needs a retransmission timeout that follows the real round trip time:
too short and it resends segments that were only slow, too long and it waits
for nothing after a loss. This module keeps the running estimate of slides
3-81 to 3-83 and the back-off of slide 3-84.
"""

# WHY an estimator: the book (3.5.3) says the timeout must be longer than
# the RTT, but not too much longer. Too short gives needless
# retransmissions, too long makes loss recovery slow. Because RTT varies
# from segment to segment, the timeout follows a running average of
# measured RTTs plus a safety margin from their variation.

class RttEstimator:
    """Keeps EstimatedRTT, DevRTT and the current timeout (TimeoutInterval).

    Attributes:
         estimated_rtt  smoothed round trip time, None until the first sample
         dev_rtt        smoothed deviation of the samples, None until the first sample
         rto            current timeout in seconds, starts at initial_rto
    """

    def __init__(self, initial_rto, min_rto, max_rto, alpha=0.125, beta=0.25):
        """Creates an estimator that has not seen any sample yet.

        Args:
             initial_rto  timeout used before the first sample, in seconds
             min_rto      the timeout never goes below this
             max_rto      the timeout never goes above this
             alpha=0.125  weight of the new sample in EstimatedRTT (slide 3-82)
             beta=0.25    weight of the new deviation in DevRTT (slide 3-83)
        """
        self.initial_rto = initial_rto
        self.min_rto = min_rto
        self.max_rto = max_rto
        # Book 3.5.3: EstimatedRTT = (1 - alpha) * EstimatedRTT +
        # alpha * SampleRTT, with alpha = 1/8. An exponentially weighted
        # moving average: new samples count most, old ones fade out.
        # DevRTT = (1 - beta) * DevRTT + beta * |SampleRTT - EstimatedRTT|,
        # with beta = 1/4: how much the RTT varies around its average.
        self.alpha = alpha
        self.beta = beta
        self.estimated_rtt = None
        self.dev_rtt = None
        # Book 3.5.3: before any SampleRTT exists, TimeoutInterval is 1
        # second (the value in the RFCs). It is replaced at the first sample.
        self.rto = initial_rto

    def add_sample(self, sample_rtt):
        """Folds one measured round trip time into the estimates.

        EstimatedRTT is an exponentially weighted moving average of the samples
        (slide 3-82): old samples count less and less. DevRTT does the same for
        how far a sample is from EstimatedRTT (slide 3-83), so a jumpy network
        gets a bigger safety margin. The timeout is EstimatedRTT + 4 * DevRTT
        (slide 3-83), kept between min_rto and max_rto.

        The first sample has nothing to average with, so EstimatedRTT = sample
        and DevRTT = sample / 2. This start comes from RFC 6298, not from the
        slides. DevRTT is updated before EstimatedRTT so that it uses the old
        EstimatedRTT, as the formula on slide 3-83 does.

        Args:
             sample_rtt  measured round trip time in seconds

        Returns:
             the new timeout in seconds
        """
        # The first sample starts the average: EstimatedRTT = sample and
        # DevRTT = sample / 2. This start rule is from RFC 6298, the book
        # does not give one. DevRTT is updated first so it uses the old
        # EstimatedRTT, as in the formula.
        if self.estimated_rtt is None:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.dev_rtt = ((1 - self.beta) * self.dev_rtt
                            + self.beta * abs(sample_rtt - self.estimated_rtt))
            self.estimated_rtt = ((1 - self.alpha) * self.estimated_rtt
                                  + self.alpha * sample_rtt)
        # Book 3.5.3: TimeoutInterval = EstimatedRTT + 4 * DevRTT. WHY 4:
        # a large margin when the RTT is jumpy, a small one when it is
        # stable. The clamp to min_rto and max_rto is a practical choice
        # (not in the book): a floor so the timer is not shorter than the
        # clock can handle, a ceiling so we never wait too long.
        timeout = self.estimated_rtt + 4 * self.dev_rtt
        self.rto = min(max(timeout, self.min_rto), self.max_rto)
        return self.rto

    def back_off(self):
        """Doubles the timeout after a timeout event.

        When the timer expires, the network is probably busy, so the sender
        waits twice as long before the next try (slide 3-84). The doubled value
        is capped at max_rto. EstimatedRTT and DevRTT are not touched: the next
        sample recomputes the timeout from them.

        Returns:
             the new timeout in seconds
        """
        # Book 3.5.4: after a timeout TimeoutInterval is doubled (exponential
        # backoff). It is NOT recomputed from the estimate, so a retransmit
        # does not hurt the average. The next fresh sample brings it back.
        self.rto = min(self.rto * 2, self.max_rto)
        return self.rto
