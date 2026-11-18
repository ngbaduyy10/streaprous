"""
StreapRous loss injector
Decides which data segments the server pretends were lost, for the tests.
"""

import random


class LossInjector:
    """Drops chosen or random data segments on the send side.

    Args:
         drop_send  numbers of first transmissions to drop
         rate       probability of dropping each sent data segment
         seed       seed of the random generator
    """

    def __init__(self, drop_send=frozenset(), rate=0.0, seed=None):
        """Sets up the drop rules.

        Args:
             drop_send  numbers n of the first transmissions to drop
             rate       probability of dropping each sent data segment
             seed       seed of the random generator
        """
        self.drop_send = frozenset(drop_send)
        self.rate = rate
        self.rng = random.Random(seed)
        self.sent_new = 0

    def check_send(self, kind):
        """Asks whether an outgoing data segment is dropped.

        Called once per data segment (payload > 0); FINs and pure ACKs never come here.

        Args:
             kind  'new' for a first transmission, anything else for a resend

        Returns:
             'nth=<n>', 'random' or None
        """
        if kind == 'new':
            self.sent_new += 1
            if self.sent_new in self.drop_send:
                return 'nth=%d' % self.sent_new
        if self.rate > 0 and self.rng.random() < self.rate:
            return 'random'
        return None
