"""
StreapRous loss injector
Decides which data segments the server pretends were lost, for the demos.
"""

import random


class LossInjector:
    """Drops chosen or random data segments.

    Args:
         drop_send  numbers of first transmissions to drop when sending
         drop_recv  numbers of received data segments to drop
         rate       probability of dropping each sent data segment
         seed       seed of the random generator
    """

    def __init__(self, drop_send=frozenset(), drop_recv=frozenset(), rate=0.0, seed=None):
        """Sets up the drop rules.

        Args:
             drop_send  numbers n of the first transmissions to drop
             drop_recv  numbers n of the received data segments to drop
             rate       probability of dropping each sent data segment
             seed       seed of the random generator
        """
        self.drop_send = frozenset(drop_send)
        self.drop_recv = frozenset(drop_recv)
        self.rate = rate
        self.rng = random.Random(seed)
        self.sent_new = 0
        self.received = 0

    def check_send(self, kind):
        """Asks whether an outgoing data segment is dropped.

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

    def check_recv(self):
        """Asks whether an incoming data segment is dropped.

        Returns:
             'nth=<n>' or None
        """
        self.received += 1
        if self.received in self.drop_recv:
            return 'nth=%d' % self.received
        return None
