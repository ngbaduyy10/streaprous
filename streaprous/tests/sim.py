"""
Simulated transfers for the TCP sender and receiver.

One TcpSender talks to one TcpReceiver over two one-way links in virtual
time: the data link carries segments from the sender to the receiver, the
ACK link carries ACKs back. Each link has a fixed delay and can drop,
delay, duplicate and reorder packets. A small reader model plays the
application that reads from the receiver. Nothing here touches sockets,
threads or the wall clock, so every run is deterministic.
"""

import heapq
import random
from collections import namedtuple

from streaprous_socket.tcp_receiver import TcpReceiver
from streaprous_socket.tcp_reno import INITIAL_SSTHRESH, RenoCongestion
from streaprous_socket.tcp_rtt import (FIRST_TIMEOUT_INTERVAL, MIN_TIMEOUT_INTERVAL,
                                       RttEstimator)
from streaprous_socket.tcp_sender import TcpSender

# One packet on a link. Data packets fill seq, payload, fin and kind (the
# SendSegment kind); ACK packets fill ack, window and reason.
Packet = namedtuple('Packet', ['seq', 'payload', 'fin', 'kind', 'ack', 'window', 'reason'])

# One recorded event: virtual time, name, details and the numbers of the
# side that raised it, taken just after the event.
Event = namedtuple('Event', ['time', 'name', 'details', 'state'])

# What run_transfer gives back.
Result = namedtuple('Result', ['delivered', 'sent', 'sender_events', 'receiver_events',
                               'time', 'sender', 'receiver', 'data_link', 'ack_link'])


class SimulationFailed(AssertionError):
    """A transfer did not finish: time limit, or nothing left to happen."""


class VirtualClock:
    """
    Simulated time in seconds; it only moves forward.

    Args:
        start: First value of ``now``.
    """

    def __init__(self, start=0.0):
        """
        Start the clock.

        Args:
            start: First value of ``now`` in seconds.
        """
        self.now = start

    def advance(self, when):
        """
        Move the clock to ``when`` unless it is already later.

        Args:
            when: Time in seconds.
        """
        if when > self.now:
            self.now = when


class Link:
    """
    One-way link with a fixed delay, losses, extra delays, copies and reordering.

    Deterministic drops count data segments (non-empty payload) from 1:
    ``drop_new`` holds indexes among first transmissions (kind 'new'),
    ``drop_any`` indexes among every data segment. ``drop_if`` is a
    callable ``drop_if(packet, now)`` for any other rule, such as one
    chosen ACK or the FIN. ``delay_if`` is a callable ``delay_if(packet,
    now)`` that returns extra delay in seconds for chosen packets (0 or
    None for none). Random loss, copies and reordering use one seeded
    generator, so a run repeats exactly.

    Args:
        delay: One-way delay in seconds.
        drop_new: Indexes of first transmissions to drop.
        drop_any: Indexes of data segments (any kind) to drop.
        drop_if: Callable that returns True for a packet to drop, or None.
        delay_if: Callable that returns extra delay for a packet, or None.
        loss: Probability of dropping any packet.
        duplicate: Probability of delivering a packet twice.
        reorder: Probability of holding a packet back by ``reorder_delay``.
        reorder_delay: Extra delay of a held-back packet; None means ``delay``.
        seed: Seed of the random generator.
    """

    def __init__(self, delay, drop_new=(), drop_any=(), drop_if=None, delay_if=None,
                 loss=0.0, duplicate=0.0, reorder=0.0, reorder_delay=None, seed=1):
        """
        Make an empty link.

        Args:
            delay: One-way delay in seconds.
            drop_new: Indexes of first transmissions to drop.
            drop_any: Indexes of data segments (any kind) to drop.
            drop_if: Callable ``drop_if(packet, now)`` or None.
            delay_if: Callable ``delay_if(packet, now)`` or None.
            loss: Probability of dropping any packet.
            duplicate: Probability of delivering a packet twice.
            reorder: Probability of holding a packet back.
            reorder_delay: Extra delay of a held-back packet; None means ``delay``.
            seed: Seed of the random generator.
        """
        self.delay = delay
        self.drop_new = frozenset(drop_new)
        self.drop_any = frozenset(drop_any)
        self.drop_if = drop_if
        self.delay_if = delay_if
        self.loss = loss
        self.duplicate = duplicate
        self.reorder = reorder
        self.reorder_delay = delay if reorder_delay is None else reorder_delay
        self.rng = random.Random(seed)
        self.queue = []
        self.order = 0
        self.new_count = 0
        self.data_count = 0
        self.sent = 0
        self.dropped = []
        self.delayed = []
        self.copies = 0
        self.held_back = 0

    def drop_reason(self, packet, now):
        """
        Decide whether a packet is lost, counting data segments on the way.

        Args:
            packet: The Packet.
            now: Current virtual time.

        Returns:
            str or None: Why it is dropped, or None when it goes through.
        """
        why = None
        if packet.payload:
            self.data_count += 1
            if packet.kind == 'new':
                self.new_count += 1
                if self.new_count in self.drop_new:
                    why = 'new=%d' % self.new_count
            if why is None and self.data_count in self.drop_any:
                why = 'any=%d' % self.data_count
        if why is None and self.drop_if is not None and self.drop_if(packet, now):
            why = 'rule'
        if why is None and self.loss > 0 and self.rng.random() < self.loss:
            why = 'random'
        return why

    def send(self, packet, now):
        """
        Put one packet on the link, or drop it.

        Args:
            packet: The Packet.
            now: Current virtual time.

        Returns:
            bool: True when the packet will arrive, False when it was dropped.
        """
        self.sent += 1
        why = self.drop_reason(packet, now)
        if why is not None:
            self.dropped.append((now, packet, why))
            return False
        arrive = now + self.delay
        extra = self.delay_if(packet, now) if self.delay_if is not None else None
        if extra:
            self.delayed.append((now, packet, extra))
            arrive += extra
        if self.reorder > 0 and self.rng.random() < self.reorder:
            self.held_back += 1
            arrive += self.reorder_delay
        self.schedule(arrive, packet)
        if self.duplicate > 0 and self.rng.random() < self.duplicate:
            self.copies += 1
            self.schedule(arrive, packet)
        return True

    def schedule(self, when, packet):
        """
        Queue a packet to arrive at ``when``; equal times keep sending order.

        Args:
            when: Arrival time.
            packet: The Packet.
        """
        heapq.heappush(self.queue, (when, self.order, packet))
        self.order += 1

    def next_time(self):
        """
        Give the arrival time of the next packet.

        Returns:
            float or None: The time, or None when the link is empty.
        """
        return self.queue[0][0] if self.queue else None

    def pop(self):
        """
        Take the next arriving packet off the link.

        Returns:
            Packet: The packet.
        """
        return heapq.heappop(self.queue)[2]


class Reader:
    """
    The application on the receiving side.

    With ``chunk`` None it reads everything as soon as bytes are ready.
    Otherwise it reads at most ``chunk`` bytes every ``interval`` seconds,
    and once it has read ``pause_after`` bytes it waits ``pause`` seconds
    one time before going on.

    Args:
        chunk: Most bytes per read, or None to read everything at once.
        interval: Seconds between reads when ``chunk`` is set.
        pause_after: Bytes read before the one pause, or None for no pause.
        pause: Length of the pause in seconds.
        start: Time of the first read when ``chunk`` is set.
    """

    def __init__(self, chunk=None, interval=0.0, pause_after=None, pause=0.0, start=0.0):
        """
        Set the reading pattern.

        Args:
            chunk: Most bytes per read, or None to read everything at once.
            interval: Seconds between reads when ``chunk`` is set.
            pause_after: Bytes read before the one pause, or None.
            pause: Length of the pause in seconds.
            start: Time of the first read when ``chunk`` is set.
        """
        if chunk is not None and interval <= 0:
            raise ValueError("a chunked reader needs interval > 0")
        self.chunk = chunk
        self.interval = interval
        self.pause_after = pause_after
        self.pause = pause
        self.due = start
        self.total = 0
        self.paused = False

    def next_time(self, receiver, now):
        """
        Give the time of the next read.

        Args:
            receiver: The TcpReceiver.
            now: Current virtual time.

        Returns:
            float or None: When to read next, or None when there is nothing
            to read (reading everything at once).
        """
        if self.chunk is None:
            return now if receiver.buffered_data else None
        return self.due

    def read(self, receiver, now):
        """
        Read from the receiver as the pattern says.

        Args:
            receiver: The TcpReceiver.
            now: Current virtual time.

        Returns:
            bytes: The data read (may be empty).
        """
        data = receiver.read(self.chunk)
        self.total += len(data)
        if self.chunk is None:
            return data
        self.due = now + self.interval
        if (not self.paused and self.pause_after is not None
                and self.total >= self.pause_after):
            self.paused = True
            self.due = now + self.pause
        return data


class Simulation:
    """
    One transfer from a TcpSender to a TcpReceiver over two links.

    The endpoint glue does what the connection does: every segment the
    sender returns goes on the data link; on the receiving side an ACK
    ``(ack=receiver.expected_seq_num, window=receiver.rwnd())`` goes on the
    ACK link whenever ``on_segment`` or ``on_tick`` gives a reason, and after
    the application reads when ``window_update_due()`` holds. ACKs carry
    no data. With ``close`` the sender closes once all its data is ACKed.

    Args:
        data: Bytes the application writes at t = 0.
        writes: Later writes, as (time, bytes) pairs.
        mss: Largest payload of one segment.
        rcv_buffer: Receiver's RcvBuffer; also the sender's first rwnd.
            65535 by default, a Linux-like client window.
        ssthresh: First ssthresh (64 KB, slide 3-125).
        delay: One-way delay of both links when they are not given.
        data_link: Link for sender to receiver, or None for a clean one.
        ack_link: Link for receiver to sender, or None for a clean one.
        reader: Reader model, or None to read everything at once.
        close: Send and ACK a FIN after the data.
        time_limit: Virtual seconds before the run fails.
        initial_seq_num: Sequence number of the first data byte.
        first_timeout_interval: TimeoutInterval before the first SampleRTT.
        min_timeout_interval: Smallest TimeoutInterval.
    """

    def __init__(self, data, writes=(), mss=1460, rcv_buffer=65535,
                 ssthresh=INITIAL_SSTHRESH, delay=0.01, data_link=None, ack_link=None,
                 reader=None, close=False, time_limit=120.0, initial_seq_num=1000,
                 first_timeout_interval=FIRST_TIMEOUT_INTERVAL,
                 min_timeout_interval=MIN_TIMEOUT_INTERVAL):
        """
        Build both endpoints and both links.

        Args:
            data: Bytes the application writes at t = 0.
            writes: Later writes, as (time, bytes) pairs.
            mss: Largest payload of one segment.
            rcv_buffer: Receiver's RcvBuffer; also the sender's first rwnd.
            ssthresh: First ssthresh.
            delay: One-way delay of links that are not given.
            data_link: Link for sender to receiver, or None.
            ack_link: Link for receiver to sender, or None.
            reader: Reader model, or None to read everything at once.
            close: Send and ACK a FIN after the data.
            time_limit: Virtual seconds before the run fails.
            initial_seq_num: Sequence number of the first data byte.
            first_timeout_interval: TimeoutInterval before the first SampleRTT.
            min_timeout_interval: Smallest TimeoutInterval.
        """
        self.data = bytes(data)
        self.writes = sorted((when, bytes(chunk)) for when, chunk in writes)
        self.sent = self.data + b"".join(chunk for _, chunk in self.writes)
        self.close = close
        self.closed = False
        self.time_limit = time_limit
        self.clock = VirtualClock()
        self.data_link = data_link or Link(delay)
        self.ack_link = ack_link or Link(delay)
        self.reader = reader or Reader()
        self.delivered = bytearray()
        self.sender_events = []
        self.receiver_events = []
        self.sender = None

        rtt = RttEstimator(first_timeout_interval, min_timeout_interval)
        reno = RenoCongestion(mss, ssthresh, self.sender_log)
        self.sender = TcpSender(initial_seq_num, mss, rcv_buffer, reno, rtt, self.sender_log)
        self.receiver = TcpReceiver(initial_seq_num, rcv_buffer, self.receiver_log)

    def sender_state(self):
        """
        Give the sender's numbers, as the trace columns name them.

        Returns:
            dict: cwnd, ssthresh, rwnd, in_flight, send_base, next_seq_num, state.
        """
        tx = self.sender
        return {'cwnd': tx.reno.cwnd, 'ssthresh': tx.reno.ssthresh, 'rwnd': tx.rwnd,
                'in_flight': tx.in_flight(), 'send_base': tx.send_base,
                'next_seq_num': tx.next_seq_num, 'state': tx.reno.state}

    def receiver_state(self):
        """
        Give the receiver's numbers.

        Returns:
            dict: expected_seq_num, rwnd, buffered, out_of_order (bytes held),
            fin_received.
        """
        rx = self.receiver
        held = sum(len(part) for part in rx.out_of_order.values())
        return {'expected_seq_num': rx.expected_seq_num, 'rwnd': rx.rwnd(),
                'buffered': len(rx.buffered_data), 'out_of_order': held,
                'fin_received': rx.fin_received}

    def sender_log(self, name, **details):
        """
        Record an event of the sender or its Reno machine.

        Args:
            name: Event name.
            details: Event details.
        """
        if self.sender is None:
            return
        self.sender_events.append(Event(self.clock.now, name, details, self.sender_state()))

    def receiver_log(self, name, **details):
        """
        Record an event of the receiver side.

        Args:
            name: Event name.
            details: Event details.
        """
        self.receiver_events.append(Event(self.clock.now, name, details,
                                          self.receiver_state()))

    def transmit(self, segments):
        """
        Put the sender's segments on the data link.

        Args:
            segments: SendSegment values.
        """
        for seg in segments:
            packet = Packet(seg.seq, seg.payload, seg.fin, seg.kind, None, None, None)
            self.data_link.send(packet, self.clock.now)

    def send_ack(self, reason):
        """
        Send a pure ACK from the receiver with its current number and window.

        Args:
            reason: Why the ACK goes out ('delayed', 'duplicate', 'window_update', ...).
        """
        rx = self.receiver
        ack = rx.expected_seq_num
        window = rx.rwnd()
        self.ack_link.send(Packet(None, b"", False, 'ack', ack, window, reason),
                           self.clock.now)
        rx.ack_sent(window)
        self.receiver_log('ACK_SENT', ack=ack, rwnd=window, reason=reason)

    def on_data_arrival(self):
        """Hand the next data packet to the receiver and ACK when asked."""
        packet = self.data_link.pop()
        reason = self.receiver.on_segment(packet.seq, packet.payload, packet.fin,
                                          self.clock.now)
        if reason:
            self.send_ack(reason)

    def on_ack_arrival(self):
        """Hand the next ACK to the sender and send what it returns."""
        packet = self.ack_link.pop()
        self.transmit(self.sender.on_ack(packet.ack, packet.window, False, self.clock.now))

    def on_sender_timer(self):
        """Run the sender's timer and probe timer."""
        self.transmit(self.sender.on_tick(self.clock.now))

    def on_receiver_timer(self):
        """Run the receiver's delayed-ACK timer."""
        reason = self.receiver.on_tick(self.clock.now)
        if reason:
            self.send_ack(reason)

    def on_read(self):
        """Let the application read, then send a window update when due."""
        self.delivered.extend(self.reader.read(self.receiver, self.clock.now))
        if self.receiver.window_update_due():
            self.send_ack('window_update')

    def on_write(self):
        """Hand the next later write of the application to the sender."""
        _, chunk = self.writes.pop(0)
        self.transmit(self.sender.send_data(chunk, self.clock.now))

    def sender_deadline(self):
        """
        Give the earliest of the sender's two timers.

        Returns:
            float or None: The time, or None when both are stopped.
        """
        times = [t for t in (self.sender.timer_deadline, self.sender.probe_deadline)
                 if t is not None]
        return min(times) if times else None

    def receiver_deadline(self):
        """
        Give the time of the receiver's delayed ACK.

        Returns:
            float or None: The time, or None when no ACK is pending.
        """
        return self.receiver.ack_deadline if self.receiver.ack_pending else None

    def finished(self):
        """
        Tell whether the transfer is complete.

        Returns:
            bool: True when the application read every byte, the sender has
            every byte ACKed and, with ``close``, the FIN was received and ACKed.
        """
        if self.writes or len(self.delivered) < len(self.sent):
            return False
        if not self.sender.data_acked():
            return False
        if self.close:
            return self.sender.fin_acked() and self.receiver.fin_received
        return True

    def next_action(self):
        """
        Pick what happens next in virtual time.

        Equal times go in this order: data arrival, ACK arrival, sender
        timers, receiver timer, application read, application write.

        Returns:
            tuple: (time, action), or (None, None) when nothing is left.
        """
        now = self.clock.now
        candidates = (
            (self.data_link.next_time(), self.on_data_arrival),
            (self.ack_link.next_time(), self.on_ack_arrival),
            (self.sender_deadline(), self.on_sender_timer),
            (self.receiver_deadline(), self.on_receiver_timer),
            (self.reader.next_time(self.receiver, now), self.on_read),
            (self.writes[0][0] if self.writes else None, self.on_write),
        )
        best = (None, None)
        for when, action in candidates:
            if when is not None and (best[0] is None or when < best[0]):
                best = (when, action)
        return best

    def fail(self, why):
        """
        Stop the run with a summary of where it got stuck.

        Args:
            why: Short reason.

        Raises:
            SimulationFailed: Always.
        """
        tx, rx = self.sender, self.receiver
        timeouts = sum(1 for event in self.sender_events if event.name == 'TIMEOUT')
        raise SimulationFailed(
            '%s at t=%.3f: delivered %d of %d, send_base=%d next_seq_num=%d rwnd=%d '
            'cwnd=%d expected_seq_num=%d receiver rwnd=%d timeouts=%d'
            % (why, self.clock.now, len(self.delivered), len(self.sent), tx.send_base,
               tx.next_seq_num, tx.rwnd, tx.reno.cwnd, rx.expected_seq_num, rx.rwnd(),
               timeouts))

    def run(self):
        """
        Run the transfer to the end.

        Returns:
            Result: Delivered and sent bytes, both event lists, the virtual
            time used, the endpoints and the links.

        Raises:
            SimulationFailed: On the time limit, or when nothing is left to
            happen before the transfer finished.
        """
        self.transmit(self.sender.send_data(self.data, self.clock.now))
        steps = 0
        while not self.finished():
            if (self.close and not self.closed and not self.writes
                    and self.sender.data_acked()):
                self.closed = True
                self.transmit(self.sender.close(self.clock.now))
                continue
            when, action = self.next_action()
            if when is None:
                self.fail('nothing left to happen')
            if when > self.time_limit:
                self.fail('time limit %.1f s' % self.time_limit)
            steps += 1
            if steps > 1000000:
                self.fail('too many steps')
            self.clock.advance(when)
            action()
        return Result(bytes(self.delivered), self.sent, self.sender_events,
                      self.receiver_events, self.clock.now, self.sender, self.receiver,
                      self.data_link, self.ack_link)


def run_transfer(data, **options):
    """
    Run one simulated transfer.

    Args:
        data: Bytes the application writes at t = 0.
        options: Keyword arguments of ``Simulation``.

    Returns:
        Result: See ``Simulation.run``.
    """
    return Simulation(data, **options).run()
