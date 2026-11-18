# TCP features in StreapRous: a walkthrough

This is our team's guide to the TCP code we added to StreapRous for Lab 4. There are six of us in three
pairs, and each pair owns one feature: reliable data transfer, flow control, or congestion control. Everyone
reads `Before you start`, then their own pair's section, then the last two sections.

Each pair section has the same five parts: the slide rules with the function that carries each one, a
traced example whose numbers come from running our classes, the known limits, the questions we expect,
and a self-check. Slide numbers such as 3-84 are the Kurose & Ross Chapter 3 slides
(`Chapter_3_v8.0.pdf`). "Handout" means the StreapRous assignment PDF. Anything from outside these two is
marked background and listed in the last section.

## Before you start

**The mental model.** Picture two people passing numbered pages through a letterbox. The sender keeps a
copy of every page until the receiver says "I have everything before page N". If a page goes missing, the
sender sends that page again. The receiver's desk has room for only so many pages, and the hallway gets
crowded if pages come too fast. Those three worries are our three features: reliable transfer (resend
what is lost), flow control (do not flood the receiver's desk), congestion control (do not flood the
hallway, that is, the network).

**The seven words we need first.** Each builds on the one before.

1. **Byte stream.** HTTP hands TCP one long row of bytes. TCP must deliver the same bytes, in the same
   order, with no "message boundaries" (3-77): the receiver gets bytes, not the sender's send calls.
2. **Segment.** The network carries small packets, so TCP cuts the stream into pieces. One piece plus a
   20-byte TCP header is a segment (3-78).
3. **MSS**, maximum segment size (3-77): the largest payload of one segment. Ours comes from the client's
   SYN, at most 1460 bytes (1500-byte Ethernet packet minus 20 bytes IP and 20 bytes TCP, background).
4. **Seq #** (3-78, 3-79): the stream number of the first byte in a segment. TCP counts bytes, not
   segments. The teacher's handshake gives our SYN the number 2000, the SYN uses one number, so our first
   data byte is 2001.
5. **ACK #** (3-79): the receiver's answer, "the seq # of the next byte I expect". It is cumulative: ACK
   2101 means "I have every byte below 2101". Easy to confuse: it is not "the last byte I got", it is one
   past it. On 3-80 host A sends `Seq=42, data='C'` and host B answers `ACK=43`.
6. **In flight** (3-79, 3-122): bytes sent but not yet ACKed. The sender keeps `send_base` (SendBase, the
   oldest unACKed byte) and `next_seq_num` (NextSeqNum, the next new byte, 3-146), so in flight is
   `next_seq_num - send_base`, the slides' LastByteSent - LastByteAcked.
7. **Window**: a cap on the bytes in flight. `rwnd` is the receiver's free buffer space (flow control);
   `cwnd` is the sender's guess of what the network carries (congestion control).

A tiny example. We send three segments of 100 bytes from seq 2001: they hold bytes 2001..2100,
2101..2200 and 2201..2300. The client answers ACK 2201, so it has the first two. Now `send_base` = 2201,
`next_seq_num` = 2301, in flight = 2301 - 2201 = 100 bytes, the third segment.

**How our code is cut.** `TcpSender` (`tcp_sender.py`), `TcpReceiver` (`tcp_receiver.py`),
`RttEstimator` (`tcp_rtt.py`) and `RenoCongestion` (`tcp_reno.py`) never touch a socket, a thread or the
clock: the caller passes in numbers and the time `now` and gets back the segments to send. That is why
every slide rule runs as a unit test on Windows, and why our traced examples below are real runs.
`TcpConnection` (`tcp_connection.py`) is the glue: one worker thread per connection reads the teacher's
per-connection packet queue, feeds ACK numbers to the sender and data to the receiver, and runs the
timers every 10 ms. Sequence numbers inside our classes are plain integers that keep growing;
`tcp_segment.py:wrap` cuts them to 32 bits for the wire and `unwrap` turns a wire value back.

**The one loop where the three features meet.** All six of us should be able to draw this:

```text
client segment --> TcpConnection.process_packet
                     |-- ACK field + window --> TcpSender.on_ack           (reliable: what is ACKed)
                     |                            |-- update_rwnd          (flow: rwnd from the client)
                     |                            |-- reno.on_new_ack / on_duplicate_ack  (congestion)
                     |                            '-- send_allowed: room = min(cwnd, rwnd) - in_flight()
                     |-- data + FIN --------> TcpReceiver.on_segment      (reliable: 3-85 ACK rules)
                     v
               TcpConnection.put_on_wire(seq, payload, fin)
                 every segment we send carries ACK = expected_seq_num and rwnd = our free space (3-94)
```

`TcpSender.send_allowed` is 3-125's "transmit new segment(s), as allowed". Each new segment is at most
one MSS and at most `min(cwnd, rwnd) - in_flight()` bytes. With MSS 1460, cwnd 4380 and rwnd 65535:

```text
room = min(4380, 65535) - 0    = 4380 -> send 1460 (in flight 1460)
room = 4380 - 1460             = 2920 -> send 1460 (in flight 2920)
room = 4380 - 2920             = 1460 -> send 1460 (in flight 4380)
room = 4380 - 4380             = 0    -> stop and wait for an ACK
```

Here cwnd is the smaller cap, so congestion control decides. When the client reads slowly, rwnd is the
smaller cap and flow control decides. Reliable transfer decides which bytes go: new ones, or the oldest
unACKed segment again after a loss.

**Reading our log.** Each event prints one line, and every event is also a row of the CSV trace
`out/trace_<client_ip>_<client_port>.csv` (columns `time,event,cwnd,ssthresh,rwnd,in_flight,send_base,`
`next_seq_num,adv_rwnd`):

```text
[Streap_tcp] <client_ip>:<port> <EVENT> key=value ... | cwnd=<n> ssthresh=<n> rwnd=<n> flight=<n> adv=<n>
```

`rwnd` is the client's window, `adv` the window we advertise. `SEND`, `ACK`, `RTT`, `RECV`, `APP_READ`
and `ACK_SENT` go to the CSV only, since one line per segment would flood the log.

**The two test commands**, run from the repo root. The first needs no network and no root; it runs every
slide rule, every scenario of our flaw hunt, and whole transfers over a simulated lossy link
(`tests/sim.py`) in virtual time:

```sh
python -m unittest discover -s streaprous/tests -t streaprous
```

The second runs our server in Docker against a real Linux client (background: the client is Linux TCP),
with ten scenarios named `lab3`, `download`, `http`, `loss`, `random-loss`, `slow-reader`, `trace`,
`upload`, `slow-app` and `py36`. It prints `PASS <scenario>: <check>` lines and ends with `ALL PASSED`;
`--only loss,trace` runs a subset:

```sh
python streaprous/docker/lab.py test
```

## Reliable data transfer

**Mental model.** IP, the layer under TCP, may lose a packet, deliver two packets in the wrong order, or
deliver one twice. Reliable transfer turns that into a perfect byte stream. The sender numbers every byte,
keeps a copy until it is ACKed, and has two ways to notice a loss: a timer that runs out (3-84), and three
duplicate ACKs (3-88), which arrive faster. The receiver puts bytes in order, keeps segments that arrive
early, and picks when to ACK by the four rows of 3-85. The timer's length comes from the measured round
trip time (3-81 to 3-83): too short resends segments that were not lost, too long reacts slowly to a loss.
Without all this the client would get a response with holes in it, and nothing would ever repair them.

**Our code.** `tcp_sender.py`: `send_data`, `new_segment`, `on_ack`, `on_new_ack`, `on_duplicate_ack`,
`fast_retransmit`, `on_timeout`, `resend_oldest`, `start_timer`, `take_rtt_sample`, `close`.
`tcp_rtt.py`: `RttEstimator`. `tcp_receiver.py`: `on_segment`, `is_old_segment`, `store_out_of_order`,
`deliver_in_order`, `choose_ack`, `on_tick`. `tcp_segment.py`: `unwrap`. `tcp_connection.py`:
`process_packet`, `send_ack`, `put_on_wire`. Tests: `test_tcp_sender.py`, `test_tcp_receiver.py`,
`test_tcp_rtt.py`, `test_tcp_segment.py`, `test_tcp_connection.py`, `test_transfer_sim.py`.

### Slide rules

| Slide | The slide's words | Our function | Mark |
| --- | --- | --- | --- |
| 3-79, 3-80 | Sequence numbers: byte stream "number" of first byte in segment's data. Acknowledgements: seq # of next byte expected from other side; cumulative ACK | `tcp_sender.py:TcpSender.new_segment`, `tcp_receiver.py:TcpReceiver.deliver_in_order`, `tcp_connection.py:TcpConnection.put_on_wire` | as the slide |
| 3-81 | SampleRTT: measured time from segment transmission until ACK receipt; ignore retransmissions | `tcp_sender.py:TcpSender.take_rtt_sample`, `tcp_sender.py:TcpSender.resend_oldest` | as the slide |
| 3-82 | `EstimatedRTT = (1 - alpha)*EstimatedRTT + alpha*SampleRTT`; typical value: alpha = 0.125 | `tcp_rtt.py:RttEstimator.update_estimated_rtt` | as the slide |
| 3-83 | `DevRTT = (1 - beta)*DevRTT + beta*\|SampleRTT - EstimatedRTT\|` (typically, beta = 0.25); `TimeoutInterval = EstimatedRTT + 4*DevRTT` | `tcp_rtt.py:RttEstimator.update_dev_rtt`, `tcp_rtt.py:RttEstimator.update_timeout_interval` | as the slide |
| 3-84 | event: data received from application: create segment with seq #; start timer if not already running (timer for oldest unACKed segment, expiration interval TimeoutInterval) | `tcp_sender.py:TcpSender.send_data`, `tcp_sender.py:TcpSender.new_segment`, `tcp_sender.py:TcpSender.start_timer` | as the slide |
| 3-84 | event: timeout: retransmit segment that caused timeout; restart timer | `tcp_sender.py:TcpSender.on_timeout`, `tcp_sender.py:TcpSender.resend_oldest` | as the slide |
| 3-84 | event: ACK received: if ACK acknowledges previously unACKed segments, update what is known to be ACKed; start timer if there are still unACKed segments | `tcp_sender.py:TcpSender.on_ack`, `tcp_sender.py:TcpSender.on_new_ack` | as the slide |
| 3-85 | in-order segment, all data ACKed: delayed ACK, wait up to 500ms for next segment, if no next segment send ACK; in-order segment, one other segment has ACK pending: immediately send single cumulative ACK; out-of-order segment, gap detected: immediately send duplicate ACK, indicating seq. # of next expected byte; segment that fills gap: immediate send ACK, provided that segment starts at lower end of gap | `tcp_receiver.py:TcpReceiver.on_segment`, `tcp_receiver.py:TcpReceiver.choose_ack`, `tcp_receiver.py:TcpReceiver.on_tick`, `tcp_receiver.py:TcpReceiver.store_out_of_order` | as the slide |
| 3-86, 3-87 | lost ACK scenario; premature timeout; cumulative ACK covers for earlier lost ACK; SendBase moves to each new ACK | `tcp_receiver.py:TcpReceiver.is_old_segment`, `tcp_sender.py:TcpSender.on_new_ack` | as the slide |
| 3-88 | if sender receives 3 additional ACKs for same data ("triple duplicate ACKs"), resend unACKed segment with smallest seq # | `tcp_sender.py:TcpSender.on_duplicate_ack`, `tcp_sender.py:TcpSender.fast_retransmit` | as the slide |
| 3-85 | The delayed ACK waits 100 ms, inside "wait up to 500ms" | `tcp_receiver.py:TcpReceiver.choose_ack` | as the slide |
| 3-79, 3-85 | Out-of-order segments are kept ("up to implementor") and delivered when the gap fills | `tcp_receiver.py:TcpReceiver.store_out_of_order`, `tcp_receiver.py:TcpReceiver.deliver_from_out_of_order` | as the slide |
| 3-84, 3-86, 3-146 | A resend repeats exactly the oldest unACKed segment as it was first sent ("Seq=92, 8 bytes of data"); after a partial ACK, only the rest of that segment | `tcp_sender.py:TcpSender.resend_oldest`, `tcp_sender.py:TcpSender.on_new_ack` | as the slide |
| 3-103, 3-104, 3-84 | Our FIN ("send TCP segment with FIN bit = 1") takes one seq # ("FINbit=1, seq=x" is answered by "ACKnum=x+1"), and the timer resends it until it is ACKed | `tcp_sender.py:TcpSender.close`, `tcp_sender.py:TcpSender.new_fin`, `tcp_sender.py:TcpSender.resend_oldest` | as the slide |
| 3-80 | A pure ACK carries seq # NextSeqNum, like "Seq=43, ACK=80" | `tcp_connection.py:TcpConnection.send_ack` | as the slide |
| 3-84 | TimeoutInterval is 1 s before the first SampleRTT | `tcp_rtt.py:RttEstimator.__init__` | extra: the timer needs a value before any ACK has come back, and the slides give none; 1 s is the first value of RFC 6298 (background) |
| 3-82, 3-83 | The first SampleRTT starts both averages: EstimatedRTT = SampleRTT, DevRTT = SampleRTT/2 | `tcp_rtt.py:RttEstimator.add_sample` | extra: each average needs an old value the first time it runs; this is the start of RFC 6298 (background) |
| 3-83 | TimeoutInterval is never below 0.2 s | `tcp_rtt.py:RttEstimator.update_timeout_interval` | extra: Linux clients delay an ACK at most 200 ms and our receiver 100 ms (background); that delay feeds SampleRTT, and a shorter timer would resend segments that were not lost (3-81 "too short: premature timeout") |
| 3-81 | One segment is timed at a time | `tcp_sender.py:TcpSender.new_segment` | extra: the code must choose which segments to time; one start time is the simplest choice, and the textbook's (background) |
| 3-84 | An ACK for bytes we never sent is ignored | `tcp_sender.py:TcpSender.on_ack` | extra: it cannot be a real ACK; taking it would mark unsent bytes as delivered, so they would never be sent |
| 3-88 | A duplicate ACK is one with ACK = SendBase, while data is in flight, that carries no data and the same window | `tcp_sender.py:TcpSender.on_ack` | extra: the code must define a duplicate ACK before it can count them; 3-88 reads one as a segment received after a missing one, while a window update or a data segment means something else (RFC 5681 defines it so, background) |
| 3-84 | Give up after 10 s without any segment from the client while data waits | `tcp_connection.py:TcpConnection.process_timers` | extra: without it a vanished client keeps the timer resending and the connection's thread waits forever |
| 3-77 | MSS = the client's MSS option, at most 1460; 536 without the option | `tcp_connection.py:choose_mss` | extra: 3-77 names MSS but gives no value, and a segment larger than the client asked for may never arrive (536 is the TCP default, background) |
| 3-103 | Wait up to 2 s after our FIN is ACKed, so the client's FIN gets its ACK | `tcp_connection.py:TcpConnection.close` | extra: 3-103 says "respond to received FIN with ACK", so our side must stay alive a moment; 2 s bounds that wait |

### Traced example

We ran our own `TcpSender`, `TcpReceiver`, `RttEstimator` and `RenoCongestion` by hand, with MSS 100, our
first byte 1000, a one-way delay of 0.05 s, and a 10 ms tick like the worker's. The application reads at
once, so the window stays 4096. A is our sender, B the receiver.

**Part A: a loss while the window is small, so the timer must recover it.**

```text
t=0.00  A sends seq=1000 len=100        cwnd=100 (1 MSS), timer runs out at 1.00 (TimeoutInterval 1 s)
t=0.05  B gets 1000..1099 in order: 3-85 row 1, wait for a next segment
t=0.16  B: no next segment, the 100 ms wait is over at the next tick: ACK=1100
t=0.21  A gets ACK=1100: SendBase=1100, SampleRTT=0.210, EstimatedRTT=0.210, DevRTT=0.105,
        TimeoutInterval = 0.210 + 4*0.105 = 0.630; cwnd=200; timer restarted, runs out at 0.84
        A sends seq=1100 len=100   -> LOST
        A sends seq=1200 len=100
t=0.26  B gets 1200: out of order, B stores it, duplicate ACK=1100 at once (3-85 row 3)
t=0.31  A gets ACK=1100: duplicate ACK number 1. Nothing else is in flight, so no more will come
t=0.84  timer runs out: A resends seq=1100 len=100, exactly the lost segment; Reno: ssthresh=100, cwnd=100
t=0.89  B gets 1100: it fills the gap, B delivers 1100..1299 and ACKs at once: ACK=1300 (3-85 row 4)
t=0.94  A gets ACK=1300: no SampleRTT, because 1100 was retransmitted (3-81); cwnd=200
t=1.04  A gets ACK=1500: SampleRTT=0.100, EstimatedRTT=0.196, DevRTT=0.103, TimeoutInterval=0.607
```

The last line is 3-82 and 3-83 with real numbers: EstimatedRTT = 0.875 × 0.210 + 0.125 × 0.100 = 0.19625,
then DevRTT = 0.75 × 0.105 + 0.25 × |0.100 - 0.19625| = 0.10281, then TimeoutInterval = 0.19625 +
4 × 0.10281 = 0.6075. One fast sample moves the estimate only an eighth of the way: that is the "smoother"
of 3-81.

**Part B: a loss while the window is large, so three duplicate ACKs recover it.** A fresh connection ran
quietly until cwnd was 600 (6 segments). Then we dropped the next new segment.

```text
t=0.61  A gets ACK=2100, 2300, 2500 (cwnd grows to 900) and sends 2500 (LOST), 2600, ..., 3300
t=0.66  B gets 2600, 2700, ..., 3300: each is out of order, each gets duplicate ACK=2500 at once
t=0.71  A gets duplicate ACKs 1, 2, 3 for 2500: fast retransmit of seq=2500 len=100 (3-88);
        Reno: ssthresh=450, cwnd=450+300=750, fast recovery. Duplicates 4 to 8: cwnd 850 ... 1250,
        so new segments 3400 (50 bytes), 3450, 3550, 3650 go out
t=0.76  B gets 2500: it fills the gap, B delivers everything up to 3399: ACK=3400 at once (3-85 row 4)
t=0.81  A gets ACK=3400: cwnd = ssthresh = 450, congestion avoidance; the timer (1.115) never ran out
```

**Why the two parts differ.** A duplicate ACK needs a segment that arrives after the hole. In part A only
one segment followed the hole, so only one duplicate came back and the timer had to recover it, 0.63 s
after the ACK that restarted it. In part B eight segments followed the hole, so three duplicates came back
0.10 s after the loss, long before the timer. Our `loss` scenario shows the same on a real network: the
3rd segment is lost while only 2 or 3 are in flight and costs a TIMEOUT; the 60th falls in a large window
and gets a FAST_RETRANSMIT. In both parts the resend is exactly the segment first sent (seq and length),
and NextSeqNum never moves back.

### Known limits

- **One timeout per lost segment when several segments of one window are lost.** Why we keep it: 3-84
  resends only the "segment that caused timeout", so we dropped the go-back that resent the whole window.
  Answer: "Each lost segment costs at most one timeout; nothing stalls, and the unit test counts exactly
  one resend per lost segment."
- **A FIN that arrives out of order is not stored; it waits for the client to resend it.** Why we keep it:
  a FIN counts only on an in-order segment, so no stored FIN can close the stream early. Answer: "The
  client's own timer resends its FIN, and we ACK it then."
- **We do not check checksums, our ISN is always 2000, and the SYN-ACK carries no options.** Why we keep
  it: checksums read on a raw socket arrive unfinished (offload, background), so checking them would drop
  every packet; the ISN and the SYN-ACK are the teacher's handshake, which ships "in full shape". Answer:
  "With no options the client sends 536-byte segments and no window scaling, so windows are plain bytes
  up to 65535 (background)."
- **A receiver that delays its ACK 0.2 s or more can cause one premature timeout.** Why we keep it: the
  0.2 s floor keeps loss recovery quick; Linux (background) delays an ACK at most 200 ms and our
  receiver 100 ms, and the delay feeds SampleRTT, so the timer learns it. Answer: "A premature timeout only
  resends a copy; the receiver ACKs it again (3-86 right), and nothing stalls."
- **Duplicate ACKs whose window changes are not counted.** Why we keep it: our definition of a duplicate
  ACK is a listed extra, and a window update means something else. Answer: "Then the timer recovers the
  segment instead of fast retransmit (3-84); the `loss` scenario still shows fast retransmit."
- **An old ACK reordered behind a newer one can set rwnd back to a stale value.** Why we keep it: no slide
  says which window to trust; data in flight draws more ACKs, and the timer or a probe recovers. Answer:
  "The next ACK carries the current window and puts it right."
- **When a client segment carries both a new ACK and data, our reply segments carry the older ACK number,
  and the new data waits for the 100 ms delayed ACK.** Why we keep it: `process_packet` hands the ACK to the
  sender before the data to the receiver, and 3-85 row 1 allows the wait. Answer: "The ACK arrives at most
  100 ms later, which 3-85 allows (up to 500 ms); in our HTTP scenarios the client sends its request while
  none of our data is in flight, so it never happens there."

### Questions to expect

Q: A data segment is lost. Show the two ways your code recovers it.\
A: The timer (`on_timeout` → `resend_oldest`, 3-84) or three duplicate ACKs (`on_duplicate_ack` → `fast_retransmit`, 3-88). Part A and part B of our traced example; tests `test_timeout_resends_oldest_unacked_segment`, `test_third_duplicate_ack_fast_retransmit`, `test_early_drop_times_out`, `test_drop_at_large_window_fast_retransmit`; the `loss` scenario.

Q: What exactly does a timeout resend?\
A: The oldest unACKed segment exactly as first sent: `segment_end` maps each unACKed segment's first seq # to the seq # after its last byte. In the 3-86 premature-timeout case that is Seq=92 with 8 bytes, as the slide draws it; after a partial ACK only the rest of that segment (`test_resend_never_past_next_seq_num`, `test_resend_after_small_window`).

Q: The ACK is lost, not the data (3-86 left). What happens?\
A: Our timer resends Seq=92; the receiver's `is_old_segment` sees every byte is below `expected_seq_num`, logs DUP_SEGMENT and ACKs 100 again (`test_lost_ack_scenario`).

Q: The ACK is only late, and the timer runs out first (3-86 right).\
A: We resend Seq=92, 8 bytes; ACK=100 and then ACK=120 arrive; SendBase jumps to 120, and the late copy is ACKed again as old (`test_premature_timeout_scenario`).

Q: ACK=100 is lost but ACK=120 arrives (3-87).\
A: ACKs are cumulative, so `on_new_ack` sets SendBase=120 and drops both segments from `segment_end`; nothing is resent (`test_cumulative_ack_covers_lost_ack`, `test_cumulative_ack_scenario`).

Q: Several segments of one window are lost.\
A: Each is recovered by its own fast retransmit or timeout, one resend per lost segment (`test_several_losses_in_one_window`); see Known limits.

Q: Segments arrive out of order, then the gap fills.\
A: Each early segment is stored and answered with a duplicate ACK at once (3-85 row 3); the segment that fills the gap gets an immediate ACK (row 4), and `deliver_from_out_of_order` moves the stored bytes in (`test_row3_out_of_order_gets_duplicate_ack`, `test_row4_gap_fill_gets_immediate_ack`, `test_reorder_and_duplicate`; the `upload` scenario shows OUT_OF_ORDER and DUP_ACK_SENT).

Q: The same segment arrives twice.\
A: The copy is old, so it is ACKed and thrown away; its bytes are delivered once (`test_duplicate_segment_acked_and_discarded`).

Q: An ACK arrives for bytes you never sent, or an old ACK below SendBase.\
A: `on_ack` ignores both: one above NextSeqNum is an extra rule (it cannot be real), one below SendBase is old, since 3-146 acts only when y > SendBase (`test_ack_above_next_seq_num_ignored`, `test_old_ack_ignored`).

Q: Seq numbers are 32 bits. What happens past 2^32?\
A: Inside our classes numbers keep growing (3-78 "counting bytes of data into bytestream"); `wrap` cuts them for the wire and `unwrap` picks the value closest to the one we expect (`test_sequence_numbers_wrap_past_2_32`).

Q: Can a retransmitted segment give a SampleRTT?\
A: No. `resend_oldest` cancels the timing, as 3-81 says "ignore retransmissions"; part A shows no sample at t=0.94 (`test_no_sample_after_timeout`, `test_no_sample_after_fast_retransmit`).

Q: An ACK equal to SendBase carries data, or a new window. Is it a duplicate?\
A: No: our definition needs no data and the same window, so it only updates rwnd (`test_ack_with_data_is_not_duplicate`, `test_window_change_is_not_duplicate`).

Q: The client vanishes in the middle of a transfer.\
A: After 10 s with no segment while data waits, `process_timers` gives up, logs GIVE_UP and `send` returns -1 (`test_silent_client_gives_up`). That test jumps 9.9 s in one tick, so it sees one timeout instead of about nine; it checks the give-up rule, not the count.

Q: Our FIN is lost; the client's FIN carries data; the client's FIN arrives out of order.\
A: Our FIN takes one seq #, so the timer resends it like data (`test_lost_fin_is_resent`); data before a FIN is delivered first (`test_fin_with_data`); an out-of-order FIN waits for the client's resend (`test_fin_out_of_order_waits_for_resend`).

Q: Two clients at once, or a segment for another port, a SYN, or an RST?\
A: Each connection has its own `TcpConnection`, worker and timers (`test_two_connections_keep_separate_state`); other ports and SYNs are ignored (`test_other_ports_and_syn_ignored`); an RST breaks the connection and `send` returns -1 (`test_send_returns_minus_one_after_rst`).

Q: Data arrives before the handshake's last ACK.\
A: The client's first byte is the SYN's seq # + 1, noted in `accept`, not the seq # of whatever packet came third (`test_first_byte_follows_syn`).

Q: A delayed ACK is pending and we send a data segment. Is the ACK sent twice?\
A: No: every segment carries our ACK number, so `put_on_wire` clears the pending ACK (`test_ack_sent_clears_pending`, `test_data_segment_carries_pending_ack`). A client segment with an ACK and data is handled ACK first (`test_client_segment_with_ack_and_data`).

Q: One lone segment meets the client's delayed ACK. Does it time out?\
A: No: the 0.2 s floor stays above the client's delay, so `download` has TIMEOUT 0 (`test_lone_segment_no_premature_timeout`).

### Self-check

1. In part A, why did the timer run out at 0.84 and not at 1.00? (At 0.21 the new ACK restarted it with TimeoutInterval 0.63.)
2. Why did part A get only one duplicate ACK? (Only one segment, 1200, followed the hole.)
3. A sends 1000..1099 and 1100..1199; the client answers ACK=1200 only. What is SendBase? (1200, ACKs are cumulative.)
4. Which 3-85 row answers a segment that fills a gap, and how fast? (Row 4, at once.)
5. Why does a retransmitted segment give no SampleRTT? (Its ACK could belong to either copy, 3-81.)

## Flow control

**Mental model.** The receiver's TCP keeps arriving bytes in a buffer of RcvBuffer bytes until the
application reads them (3-90 to 3-94). If the network delivers faster than the application reads, the
buffer fills, and any further byte would have to be thrown away and sent again. So the receiver tells the
sender how much room is left, `rwnd` = RcvBuffer - buffered data, in the window field of every segment it
sends, and the sender keeps its in-flight bytes at or below that rwnd. Our server plays both parts: as
receiver for an upload (we advertise our rwnd), as sender for a download (we obey the client's rwnd).

```text
|<----------------------------- RcvBuffer = 4096 ----------------------------->|
|  buffered data (in order, not yet read)  |        free buffer space = rwnd     |
```

**Our code.** `tcp_receiver.py`: `rwnd`, `trim_to_window`, `read`, `window_update_due`.
`tcp_sender.py`: `send_allowed`, `update_rwnd`, `check_probe_timer`, `send_probe`. `tcp_connection.py`:
`put_on_wire` (the window field), `recv`. Tests: `test_tcp_receiver.py`, `test_tcp_sender.py`,
`test_transfer_sim.py`.

### Slide rules

| Slide | The slide's words | Our function | Mark |
| --- | --- | --- | --- |
| 3-94 | TCP receiver "advertises" free buffer space in rwnd field in TCP header; sender limits amount of unACKed ("in-flight") data to received rwnd | `tcp_receiver.py:TcpReceiver.rwnd`, `tcp_connection.py:TcpConnection.put_on_wire`, `tcp_sender.py:TcpSender.update_rwnd`, `tcp_sender.py:TcpSender.send_allowed` | as the slide |
| 3-94 | "guarantees receive buffer will not overflow": the sender's limit does it, and our receiver also cuts any data past the right edge of rwnd | `tcp_receiver.py:TcpReceiver.trim_to_window` | as the slide |
| 3-94 | RcvBuffer = 4096 bytes ("typical default is 4096 bytes"), set by `STREAPROUS_RCVBUF` ("set via socket options") | `tcp_config.py:load_settings` | as the slide |
| 3-90 | Application removing data from TCP socket buffers | `tcp_receiver.py:TcpReceiver.read`, `tcp_connection.py:TcpConnection.recv` | as the slide |
| 3-94 | With rwnd = 0 and nothing in flight, we send one data byte every 0.5 s | `tcp_sender.py:TcpSender.send_probe`, `tcp_sender.py:TcpSender.check_probe_timer` | extra: with rwnd 0 we send nothing, so no ACK comes back; if the receiver's window update is lost, both sides wait forever (one-byte probes are the textbook's, background) |
| 3-94 | After the application reads, one ACK when the free space grows from below a quarter of RcvBuffer to at least a quarter | `tcp_receiver.py:TcpReceiver.window_update_due` | extra: the receiver advertises rwnd only in segments it sends, so after a zero or small window nothing else tells the sender that space opened |

### Traced example

We ran `TcpReceiver` with RcvBuffer 4096 and a slow reader: 1000-byte segments arrive from seq 1, and the
application reads 1000 bytes now and then. Then we ran `TcpSender` against a closed window (we set cwnd to
8000 by hand, so only rwnd limits it).

```text
t=0.00  segment 1..1000       -> wait (3-85 row 1)       buffered=1000  rwnd()=3096
t=0.01  segment 1001..2000    -> cumulative ACK=2001     buffered=2000  rwnd=2096 advertised
t=0.02  segment 2001..3000    -> wait                    buffered=3000  rwnd()=1096
t=0.03  segment 3001..4000    -> cumulative ACK=4001     buffered=4000  rwnd=96 advertised
t=0.04  segment 4001..5000    -> OUT_OF_WINDOW: only 4001..4096 fit, the rest is cut   rwnd()=0
t=0.15  delayed ACK=4097, rwnd=0
t=0.50  app reads 1000        -> rwnd()=1000, below a quarter (1024): no window update yet
t=1.00  app reads 1000        -> rwnd()=2000, at least a quarter: WINDOW_UPDATE, ACK=4097 rwnd=2000
t=1.05  segment 4097..5096    -> wait                    rwnd()=1000
--- sender side: the client advertises 4096, then 0
t=0.00  sends 1, 1001, 2001, 3001 (1000 bytes each) and 4001 (96 bytes): in flight = 4096 = rwnd
t=0.20  ACK=4097 window=0     -> RWND_ZERO; nothing in flight, data waits: probe timer set for 0.70
t=0.70  PROBE: one byte at seq 4097
t=0.75  ACK=4097 window=0     -> still closed; the next probe at 1.20
t=1.25  ACK=4097 window=2000  -> RWND_OPEN; sends 4097 (1000 bytes) and 5097 (the last 904 bytes)
```

**What to see.** rwnd only counts in-order bytes the application has not read (out-of-order bytes lie
inside the window already advertised). The fifth segment did not fit, so `trim_to_window` kept the 96
bytes that did; a correct sender never sends past rwnd, as the sender side shows with its 96-byte segment.
The first read freed 1000 bytes, under a quarter of 4096, so no ACK went out: a tiny update would only
let the sender send a tiny segment. The probe exists because, with rwnd 0 and nothing in flight, the
sender gets no ACKs; if our WINDOW_UPDATE were lost, only the probe's answer would bring the new window.
On the real network, `slow-reader` shows RWND_ZERO, PROBE and RWND_OPEN, and `slow-app` shows our own
window reaching 0 and reopening. `slow-app` uses `STREAPROUS_RCVBUF=16080` and
`STREAPROUS_APP_READ_BYTES=2144`: the client sends 536-byte pieces; 16080 is exactly 30 of them and each
read frees exactly 4, so the free space is always whole pieces and can drop to exactly 0.

### Known limits

- **The teacher's dispatcher queue holds 100 packets and drops silently when full.** Why we keep it: the
  fix is in the teacher's dispatcher; RcvBuffer 4096 lets the client send at most 8 of its 536-byte
  segments, and our sends draw at most one ACK per segment, so the queue stays far below 100. Answer: "A
  drop there looks like a loss, and our timer or the client's resends it."
- **`run_conn_send` blocks until every byte is ACKed.** Why we keep it: the backend answers one request per
  connection, so waiting costs nothing here. Answer: "A server that sends while it reads would need a
  separate send buffer; ours never does."
- **With RcvBuffer at or below one client segment, every segment waits the 100 ms delayed ACK.** Why we
  keep it: 3-85 row 2 needs a second in-order segment, and only one fits. Answer: "That follows 3-85
  row 1; the default 4096 holds 7 of the client's 536-byte segments, so it only shows if
  `STREAPROUS_RCVBUF` is set below about 1 KB."
- **If a receiver shrinks its window to 0 while data is in flight, the timer resends into the closed
  window and Reno treats it as a loss.** Why we keep it: probes run only with nothing in flight, and a
  Linux client (background) never shrinks a window it already advertised. Answer: "Each timeout resend acts as a probe;
  when the window opens, the transfer goes on."
- **Small tail segments.** After each ACK the sender fills min(cwnd, rwnd) - in flight exactly, so a short
  segment (5 to 1458 bytes) can follow the full ones; a 1 MB download used 1558 segments instead of 719.
  Why we keep it: 3-122 counts bytes and 3-125 says "transmit new segment(s), as allowed"; waiting for a
  full segment would be an extra we removed. Answer: "We fill the window exactly, as 3-122 allows; it
  costs more packets, not correctness."

### Questions to expect

Q: rwnd is 0 and our window update is lost. Does the transfer hang?\
A: No. With rwnd 0, nothing in flight and data waiting, the sender sends one probe byte every 0.5 s, and the receiver's answer carries the new window (`test_lost_window_update_recovered_by_probe`, `test_lost_window_update`; `slow-reader` shows PROBE).

Q: rwnd is smaller than one MSS. Do you wait for a full segment?\
A: No: `send_allowed` sends min(MSS, data left, room) bytes, so a 96-byte segment goes out (`test_rwnd_below_mss_sends_small_segment`, `test_tiny_receive_buffer`).

Q: A segment does not fit the free buffer.\
A: `trim_to_window` keeps the part up to the right edge of rwnd and logs OUT_OF_WINDOW; if the end is cut, the FIN goes too and the client resends (`test_segment_beyond_rwnd_trimmed`).

Q: The window reopens while our probe byte is in the client's buffer.\
A: The ACK then covers the probe byte (probe_seq + 1), which NextSeqNum does not count; `on_ack` accepts it and `on_new_ack` moves NextSeqNum up to it (`test_ack_covering_probe_byte`).

Q: The window shrinks below what is already in flight.\
A: room = min(cwnd, rwnd) - in flight is negative, so nothing new is sent until ACKs bring in flight below rwnd (`test_shrinking_window_sends_nothing_new`).

Q: What if 100 packets pile up in the teacher's queue, or `run_conn_send` waits?\
A: See Known limits: our windows keep the queue far below 100, and waiting costs nothing with one request per connection.

Q: Why is rwnd RcvBuffer minus buffered data, and not minus the out-of-order bytes too?\
A: 3-94 draws rwnd as the free space after the buffered in-order data; out-of-order bytes sit inside the window we already advertised, so counting them would shrink a window the sender already uses.

Q: When does the receiver send a window update, and why not after every read?\
A: Once, when free space grows from below RcvBuffer/4 to at least RcvBuffer/4 (our extra); after every read would send many tiny updates, each letting the sender send a tiny segment. Our trace shows a read of 1000 bytes with no update.

### Self-check

1. RcvBuffer 4096 holds 3000 unread bytes. What rwnd goes out? (1096.)
2. In the trace, why did the read at t=0.50 send no ACK? (1000 is below a quarter of 4096, 1024.)
3. When does the sender probe? (rwnd 0, nothing in flight, data waiting; every 0.5 s.)
4. Which number limits sending when rwnd is 2000 and cwnd 8000? (rwnd: min(8000, 2000) = 2000.)

## Congestion control

**Mental model.** Flow control protects the receiver; congestion control protects the network between us.
No router tells the sender how much it can carry, so the sender guesses with `cwnd` and keeps in flight at
or below it (3-122). It probes: grow cwnd while ACKs come back, cut it when a loss says the network is
full (AIMD, 3-120). TCP Reno has three states (3-125). **Slow start** adds one MSS per new ACK, which
doubles cwnd every RTT (3-123). Above `ssthresh` it switches to **congestion avoidance**, about one MSS
per RTT. Three duplicate ACKs halve cwnd and enter **fast recovery**; a timeout drops cwnd to 1 MSS
(3-121: a timeout means more trouble, since not even duplicate ACKs came back).

**Our code.** `tcp_reno.py`: `RenoCongestion` with `on_new_ack`, `on_duplicate_ack`, `on_timeout`,
`check_slow_start_end`. `tcp_sender.py`: `send_allowed`, `in_flight`, and the calls into Reno from
`on_new_ack`, `on_duplicate_ack`, `on_timeout`. Tests: `test_tcp_reno.py` (every arrow of 3-125),
`test_tcp_sender.py`.

### Slide rules

| Slide | The slide's words | Our function | Mark |
| --- | --- | --- | --- |
| 3-122 | `LastByteSent - LastByteAcked <= cwnd` | `tcp_sender.py:TcpSender.send_allowed`, `tcp_sender.py:TcpSender.in_flight` | as the slide |
| 3-123 | initially cwnd = 1 MSS; double cwnd every RTT, done by incrementing cwnd for every ACK received | `tcp_reno.py:RenoCongestion.__init__`, `tcp_reno.py:RenoCongestion.on_new_ack` | as the slide |
| 3-124 | on loss event, ssthresh is set to 1/2 of cwnd just before loss event | `tcp_reno.py:RenoCongestion.on_timeout`, `tcp_reno.py:RenoCongestion.on_duplicate_ack` | as the slide |
| 3-125 | start: cwnd = 1 MSS, ssthresh = 64 KB, dupACKcount = 0 | `tcp_reno.py:RenoCongestion.__init__` | as the slide |
| 3-125 | slow start, new ACK: cwnd = cwnd+MSS, dupACKcount = 0, transmit new segment(s), as allowed; cwnd > ssthresh: go to congestion avoidance | `tcp_reno.py:RenoCongestion.on_new_ack`, `tcp_reno.py:RenoCongestion.check_slow_start_end`, `tcp_sender.py:TcpSender.send_allowed` | as the slide |
| 3-125 | congestion avoidance, new ACK: `cwnd = cwnd + MSS*(MSS/cwnd)`, dupACKcount = 0, transmit new segment(s), as allowed | `tcp_reno.py:RenoCongestion.on_new_ack`, `tcp_sender.py:TcpSender.send_allowed` | as the slide |
| 3-125 | slow start or congestion avoidance, duplicate ACK: dupACKcount++; dupACKcount == 3: ssthresh = cwnd/2, cwnd = ssthresh + 3, retransmit missing segment, go to fast recovery | `tcp_reno.py:RenoCongestion.on_duplicate_ack`, `tcp_sender.py:TcpSender.on_duplicate_ack`, `tcp_sender.py:TcpSender.fast_retransmit` | as the slide |
| 3-125 | fast recovery, duplicate ACK: cwnd = cwnd + MSS, transmit new segment(s), as allowed; new ACK: cwnd = ssthresh, dupACKcount = 0, go to congestion avoidance | `tcp_reno.py:RenoCongestion.on_duplicate_ack`, `tcp_reno.py:RenoCongestion.on_new_ack` | as the slide |
| 3-125 | timeout (any state): ssthresh = cwnd/2, cwnd = 1 MSS, dupACKcount = 0, retransmit missing segment, go to slow start | `tcp_reno.py:RenoCongestion.on_timeout`, `tcp_sender.py:TcpSender.on_timeout` | as the slide |
| 3-125 | After a timeout leaves cwnd = 1 MSS above a smaller ssthresh, the arrow "cwnd > ssthresh" (it needs no event) leads straight on to congestion avoidance | `tcp_reno.py:RenoCongestion.on_timeout`, `tcp_reno.py:RenoCongestion.check_slow_start_end` | as the slide |
| 3-125 | cwnd/2 and MSS*(MSS/cwnd) round down to whole bytes | `tcp_reno.py:RenoCongestion.on_new_ack`, `tcp_reno.py:RenoCongestion.on_duplicate_ack`, `tcp_reno.py:RenoCongestion.on_timeout` | extra: windows are whole bytes, and rounding down changes cwnd by less than one byte per ACK |

### Traced example

We ran `RenoCongestion` with MSS 1000 and ssthresh 8000 (smaller than 64 KB, so the switch comes soon),
and fed it events one at a time:

```text
step event               cwnd  ssthresh dupACKcount state
 1   start               1000  8000     0   SLOW_START
 2-8 new ACK x7          2000, 3000, ..., 8000 (one MSS per ACK)          SLOW_START
 9   new ACK             9000  8000     0   9000 > 8000: CONGESTION_AVOIDANCE
10   new ACK             9111  8000     0   9000 + 1000*1000//9000 = 9000 + 111
11   new ACK             9220  8000     0   + 1000000//9111 = 109
12   new ACK             9328  8000     0   + 108
13   duplicate ACK       9328  8000     1
14   duplicate ACK       9328  8000     2
15   duplicate ACK       7664  4664     3   ssthresh = 9328//2, cwnd = 4664 + 3*1000: FAST_RECOVERY
16   duplicate ACK       8664  4664     3   cwnd + MSS
17   new ACK             4664  4664     0   cwnd = ssthresh: CONGESTION_AVOIDANCE
18   timeout             1000  2332     0   ssthresh = 4664//2, cwnd = 1 MSS: SLOW_START
19   new ACK             2000  2332     0   SLOW_START
--- a fresh RenoCongestion with cwnd 1000:
20   timeout             1000   500     0   1000 > 500: straight on to CONGESTION_AVOIDANCE
21   new ACK             2000   500     0   1000 + 1000*1000//1000
```

**What to see.** Steps 2 to 8 add one MSS per ACK; since one window's worth of ACKs comes back per RTT,
cwnd doubles per RTT (3-123). Step 9 is the "cwnd > ssthresh" arrow: 8000 is not above 8000, 9000 is.
In congestion avoidance each ACK adds MSS*(MSS/cwnd), so one window's worth of ACKs adds about one MSS,
and the step shrinks as cwnd grows (111, 109, 108). Step 15 reads the slide's "cwnd = ssthresh + 3" as 3
MSS, one for each duplicate ACK, each meaning a segment has left the network (3-88). Step 18: a timeout
halves cwnd for ssthresh, then cwnd starts again from 1 MSS. Step 20 is the edge case of repeated
timeouts: ssthresh 500 is below one MSS, cwnd stays 1000, and the Λ arrow moves on to congestion
avoidance at once; nothing stalls, since cwnd never drops below 1 MSS.

On the real network the `trace` scenario (drop list `150,400-460`, 20 ms delay each way) shows slow start,
the switch, fast retransmits and timeouts; its checks read the CSV to confirm one MSS per ACK in slow start
and the cwnd after each TIMEOUT and FAST_RETRANSMIT.

### Known limits

- **cwnd grows while rwnd is the limit.** 3-123 grows cwnd on every ACK even when rwnd limits sending, so
  cwnd can sit far above what is used, and a loss halves a cwnd that was never in use. Why we keep it: it
  is the slide's rule as written. Answer: "Sending is limited by min(cwnd, rwnd), so the big cwnd sends
  nothing extra; after the halving cwnd may still be above rwnd."
- **Slow start grows about 1.5x per RTT, not 2x, against a client that ACKs every second segment.** Why we
  keep it: 3-123 says the doubling is "done by incrementing cwnd for every ACK received", which is what we
  do. Answer: "We add one MSS per ACK, as the slide says; doubling needs one ACK per segment, and 3-85
  row 2 lets the client ACK every second one."
- **An accepted probe byte grows cwnd like any new ACK.** Why we keep it: 3-123 says "every ACK received".
  Answer: "A Linux client (background) refuses probe bytes while its window is 0, so this is one byte, rarely."

### Questions to expect

Q: Why do the `trace` scenario's losses end in timeouts, not only fast retransmits?\
A: We lose 61 segments in a row. After two fast retransmits the shrunken cwnd lets nothing new out, so no duplicate ACKs come back for the next hole, and only the timer can recover it.

Q: A timeout happens during fast recovery.\
A: The timeout arrow is "any state": ssthresh = cwnd/2 of the inflated cwnd, cwnd = 1 MSS, slow start (`test_arrow_13_fast_recovery_timeout`, `test_timeout_during_fast_recovery`).

Q: After repeated timeouts ssthresh falls below one MSS. Does the sender stall?\
A: No: cwnd is 1 MSS, above ssthresh, so Reno goes straight to congestion avoidance (step 20) and one segment can always go out (`test_repeated_timeouts_ssthresh_below_mss`). We removed the 2·MSS floor: 3-124 says "1/2 of cwnd".

Q: Why is cwnd halved after three duplicate ACKs but set to 1 MSS after a timeout?\
A: 3-121: duplicate ACKs show segments still get through, so the network is only somewhat full; a timeout shows nothing came back at all.

Q: Why cwnd = ssthresh + 3 MSS on the third duplicate ACK, and + MSS for each later one?\
A: Each duplicate ACK means one more segment left the network (3-88), so the sender may put one more in; 3-125 writes it as "+ 3" and "cwnd + MSS" (the textbook says 3 MSS, background).

Q: Where is "transmit new segment(s), as allowed" in your code?\
A: Every ACK path in `TcpSender` ends in `send_allowed`, which sends while in flight is below min(cwnd, rwnd) (3-122); in fast recovery the inflated cwnd lets new segments out, as part B of the reliable example shows.

Q: How do you show slow start on a real network?\
A: `trace`: the CSV check confirms cwnd grows by exactly one MSS (1460) per new ACK in slow start, and the STATE line SLOW_START -> CONGESTION_AVOIDANCE appears.

### Self-check

1. MSS 1000, cwnd 6000, slow start: what is cwnd after 2 new ACKs? (8000.)
2. cwnd 10000 in congestion avoidance: how much does one ACK add? (1000*1000//10000 = 100.)
3. cwnd 9328, third duplicate ACK: new ssthresh and cwnd? (4664 and 7664.)
4. cwnd 7664 in fast recovery, a timeout: ssthresh, cwnd, state? (3832, 1000, slow start.)

## Our hooks in the teacher's files

Every change to the teacher's code carries a `# TEAM:` comment line; everything else keeps his names,
comments and prints. Before our changes `run_conn_send` put the whole payload in one segment, the window
was fixed at 5840, nothing was ever retransmitted, and `handle_client_raw` slept 5 s before closing.

| File | Function | What our hook does |
| --- | --- | --- |
| `streaprous_raw_connection.py` | module imports | imports `RECV_BUFFER` from `tcp_config.py`, the default window of the header builders (3-94) |
| `streaprous_raw_connection.py` | `build_tcp_packet` | a `window` argument replaces the fixed 5840, so every segment advertises our free buffer space |
| `streaprous_raw_connection.py` | `send_tcp_data_segment` | the same `window` argument for data segments |
| `streaprous_raw_connection.py` | `accept` | calls `note_client_syn`, which keeps the client's MSS option, SYN window and seq # for its connection |
| `streaprous_raw_connection.py` | `run_conn_recv` | returns the next in-order bytes from `TcpConnection.recv`; our worker reads the packet queue now |
| `streaprous_raw_connection.py` | `run_conn_send` | `TcpConnection.send` cuts segments, retransmits, and returns once every byte is ACKed, or -1 |
| `streaprous_raw_connection.py` | `close_connection` | `finish_connection` sends our FIN until it is ACKed and ACKs the client's FIN |
| `daemon/httpadapter.py` | `handle_client_raw` | reads the whole request, sends the response with `run_conn_send`, and always closes once; the 5 s sleep is gone |
| `daemon/httpadapter.py` | `read_request_raw` | new: TCP keeps no message borders (handout section 2.1), so it reads until the blank line after the headers, then Content-Length body bytes |
| `daemon/request.py` | `prepare` | fills the Lab 3 gap: splits headers from body and parses the headers |

## Background

Facts we use from outside the slides and the handout, each with where we use it. Facts marked "seen" we
saw on our own Docker network.

- **RFC 6298:** TimeoutInterval starts at 1 s (`RttEstimator.__init__`); the first sample sets the
  estimate to SampleRTT and the deviation to SampleRTT/2 (`add_sample`); it updates the deviation before
  the estimate, while we follow the slides' page order, 3-82 then 3-83 (`update_dev_rtt`).
- **RFC 5681:** the definition of a duplicate ACK (`TcpSender.on_ack`); cwnd = ssthresh + 3 segments on
  the third duplicate ACK (`RenoCongestion.on_duplicate_ack`). Slide 3-85 itself cites RFC 5681.
- **RFC 1122:** 536 bytes is the MSS when the SYN has no MSS option (`choose_mss`).
- **RFC 7323 (listed on 3-77):** without window scaling the 16-bit window field holds at most 65535
  bytes; our SYN-ACK has no options, so windows are plain bytes (reliable Known limits).
- **Ethernet:** a 1500-byte packet minus 20 bytes IP and 20 bytes TCP gives MSS 1460 (`MAX_MSS`).
- **Linux, delayed ACK and timer:** Linux delays an ACK 40 to 200 ms, and its minimum retransmission time
  is 200 ms. We use this for the 0.2 s floor (`update_timeout_interval`) and for our 100 ms delayed ACK
  (`choose_ack`), which stays below the client's 200 ms, so our delay never makes the client resend.
- **Linux, MSS and segments (seen):** the client's SYN carries MSS 1460; without an MSS option in our
  SYN-ACK it sends us 536-byte segments and no window scaling (`choose_mss`, `slow-app` values).
- **Linux, windows:** it never shrinks a window it already advertised (flow Known limits); it answers our
  one-byte probe with an ACK carrying its window (seen, `send_probe`); it refuses probe bytes while its
  window is 0 (congestion Known limits); its own zero-window probes are empty segments one below the
  expected seq #, so `TcpReceiver.on_segment` ACKs an empty segment anywhere else.
- **Linux, checksum offload (seen):** checksums read on a raw socket in Docker arrive unfinished, so we
  do not check them (reliable Known limits).
- **The textbook (Kurose & Ross, the book behind the slides):** one segment is timed at a time
  (`new_segment`); zero-window probes of one data byte (`send_probe`); cwnd = ssthresh + 3·MSS on the
  third duplicate ACK (congestion Questions to expect).
