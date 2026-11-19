# TCP features in StreapRous: a walkthrough

This is our team's guide to the TCP code we added to StreapRous. There are six of us in three pairs, and
each pair owns one feature:

| Pair | Feature | Part |
| --- | --- | --- |
| 1 | Reliable data transfer | [Part 2](#part-2-reliable-data-transfer) |
| 2 | Flow control | [Part 3](#part-3-flow-control) |
| 3 | Congestion control (TCP Reno) | [Part 4](#part-4-congestion-control-tcp-reno) |

Everyone reads Part 1 first, because it explains the words and the one loop where all three features
meet. Then read your own pair's part, then Parts 5 to 7. Every part gives the code, the slides, an example
with our real numbers, the demo that shows it, and three self-check questions.

Slide numbers such as 3-84 are the Kurose & Ross Chapter 3 slides (`Chapter_3_v8.0.pdf`, PDF page N is
slide 3-N). "Handout" means the StreapRous assignment PDF. Anything we took from outside these two is
marked **Background** and listed in [Part 6](#part-6-background-what-does-not-come-from-the-slides).

Contents

- [Part 1. Shared introduction](#part-1-shared-introduction)
- [Part 2. Reliable data transfer](#part-2-reliable-data-transfer)
- [Part 3. Flow control](#part-3-flow-control)
- [Part 4. Congestion control (TCP Reno)](#part-4-congestion-control-tcp-reno)
- [Part 5. Our hooks in the teacher's files](#part-5-our-hooks-in-the-teachers-files)
- [Part 6. Background: what does not come from the slides](#part-6-background-what-does-not-come-from-the-slides)
- [Part 7. The whole story in one picture](#part-7-the-whole-story-in-one-picture)
- [Appendix A. Every function, module by module](#appendix-a-every-function-module-by-module)
- [Appendix B. Tools, demo client and Docker driver](#appendix-b-tools-demo-client-and-docker-driver)

---

## Part 1. Shared introduction

### 1.1 The words we need first

Before any feature makes sense we need seven words. Each one builds on the one before.

1. **Byte stream.** The HTTP code hands TCP one long row of bytes, for example a response of 1 048 757
   bytes. TCP must put the same bytes, in the same order, into the program at the other end. TCP keeps
   no "message boundaries" (slide 3-77): the receiver gets bytes, not the sender's send calls.
2. **Segment.** The network carries small packets, so TCP cuts the stream into pieces. One piece plus a
   20-byte TCP header is a segment (slide 3-78). Each segment travels inside an IP packet with its own
   20-byte IP header.
3. **MSS** (maximum segment size, slide 3-77). The largest payload in one segment. Ours is 1460 bytes:
   an Ethernet link carries at most 1500 bytes per packet, and 1500 − 20 (IP) − 20 (TCP) = 1460.
4. **Sequence number** (slides 3-78, 3-79). The number of the first byte in a segment. TCP counts
   *bytes*, not segments. Our first data byte is 2001: the teacher's handshake gives the SYN number
   2000, and the SYN uses up one number.
5. **ACK number** (slide 3-79). The receiver's answer: "the next byte I expect". ACKs are
   *cumulative*: ACK 4921 means "I have every byte below 4921".
6. **In flight** (slide 3-79). Bytes we sent that are not ACKed yet. In our code that is
   `snd_nxt − snd_una`.
7. **Window.** A cap on the bytes in flight. There are two caps. `rwnd` is the receiver's free buffer
   space (flow control, Part 3). `cwnd` is the sender's guess of what the network can carry (congestion
   control, Part 4).

A tiny example with our numbers. We send three segments of 1460 bytes:

```text
segment 1: seq 2001, bytes 2001..3460
segment 2: seq 3461, bytes 3461..4920
segment 3: seq 4921, bytes 4921..6380
```

If the client answers "ACK 4921", it has segments 1 and 2. Segment 3 is still in flight:
`snd_una = 4921`, `snd_nxt = 6381`, in flight = 6381 − 4921 = 1460 bytes.

This is easy to confuse: the ACK number is **not** "the last byte I got". It is one past it, the next
byte expected. Slide 3-80 shows the same idea with one-byte segments: host A sends `Seq=42, data='C'`,
host B answers `ACK=43`.

### 1.2 What StreapRous gave us

Handout §2.1 describes three calls on a raw socket:

- `accept()` completes the TCP three-way handshake and returns `conn_info`, a `conn_struct` record with
  the client and server address and port, the state, `server_seq`, `client_seq` and the time of the last
  activity. Records live in the global `connections` table, keyed by `(client_ip, client_port)`.
- `run_conn_recv()` reads the bytes that are available. The caller processes them "as stream chunks";
  an empty result means no data or a closed connection.
- `run_conn_send()` transmits the payload on the connection.

The teacher's library also has a dispatcher thread. It reads every packet from the raw socket and puts
it into a per-connection queue (`queue.Queue(maxsize=100)`).

When we read the original code, we found that it did not do any of the TCP work yet:

- `run_conn_send` put the whole payload into **one** segment. A 20 KB response failed with
  `[Streap_raw] run_conn_send Error in run_conn_send: [Errno 90] Message too long`.
- The window field was fixed at 5840.
- There was no timer and no retransmission. `handle_client_raw` slept 5 s before closing and hoped the
  data had arrived.
- `run_conn_recv` ACKed every data packet at once and returned it, whatever its sequence number. There
  was no ordering and no duplicate check.
- `close_connection` sent one FIN, once.

### 1.3 What we added

All new files are under `streaprous/streaprous_socket/`:

| File | Feature | What it holds |
| --- | --- | --- |
| `tcp_sender.py` | all three | `TcpSender`: cuts segments, one retransmission timer, fast retransmit, the min(cwnd, rwnd) limit, zero-window probe |
| `tcp_receiver.py` | reliable, flow | `TcpReceiver`: in-order delivery, out-of-order buffer, the ACK rules of slide 3-85, the receive window |
| `tcp_rtt.py` | reliable | `RttEstimator`: EstimatedRTT, DevRTT, TimeoutInterval |
| `tcp_reno.py` | congestion | `RenoCongestion`: the state machine of slide 3-125 |
| `tcp_connection.py` | glue | `TcpConnection`: worker thread, raw-socket input and output, the registry of connections |
| `tcp_segment.py` | all | parse a received packet, read the MSS option, 32-bit sequence numbers |
| `tcp_config.py` | all | settings from environment variables |
| `tcp_loss.py` | demos | `LossInjector`: drops chosen segments so we can show losses |
| `tcp_trace.py` | demos | `TcpTrace`: the `[Streap_tcp]` log lines and the CSV trace |

We also changed a few lines in the teacher's files. Each change carries a `# TEAM` comment, and Part 5
lists all of them.

`TcpSender`, `TcpReceiver`, `RttEstimator` and `RenoCongestion` never touch a socket, a thread or the
clock. The caller passes in ACK numbers, segments and the current time `now`, and gets back the
segments to send. That is why every rule has a test on Windows that needs no network.

### 1.4 One connection, the mental model

Picture one connection as two people passing numbered pages through a letterbox. The sender keeps
copies until the receiver says which page it wants next. Here is our version with the real names:

```text
  client (real Linux or Windows TCP)
        |  IP packets
        v
  raw socket --> dispatcher thread (teacher) --> queue for this connection (100 packets)
                                                         |
                                                         v
  worker thread: TcpConnection._worker  (only reader of that queue; wakes on a packet or after 10 ms)
     packet --> parse_tcp_packet
                  ACK number + window --> TcpSender.on_ack      --> segments to send --> sendto
                  data + FIN          --> TcpReceiver.on_segment --> ACK now?        --> sendto
     timers --> TcpSender.on_tick (retransmission, persist)
                TcpReceiver.on_tick (delayed ACK)

  handler thread (one per connection): HttpAdapter.handle_client_raw
     read_request_raw --> run_conn_recv --> TcpConnection.recv --> TcpReceiver.read
     build the response
     run_conn_send    --> TcpConnection.send --> TcpSender.write  (waits until all bytes are ACKed)
     close_connection --> TcpConnection.close (FIN until ACKed, then wait for the client's FIN)
```

Why a separate worker thread? ACKs must keep flowing while the application is busy or sleeping. If
the handler thread read the queue itself, a slow application would stop reading ACKs. The timers
would stop too, and nothing would be retransmitted. One worker per connection also gives each
connection its own timers. One `threading.Condition` per connection guards all of its state. The
worker holds it while it handles a packet or a tick. The handler thread holds it inside `recv`, `send`
and `close`.

The code path of one HTTP download:

```text
run_conn_send --> get_connection(...).send --> TcpSender.write --> TcpSender._send_allowed
              --> TcpConnection._transmit --> send_tcp_data_segment (teacher's builder, our window)
```

### 1.5 The send loop where the three features meet

This is the most important picture in the document. All three pairs should be able to draw it. It is
the sender's sequence space from slide 3-79, with the cwnd limit of slide 3-122 and the rwnd limit of
slide 3-94:

```text
                snd_una                     snd_nxt
                   |                           |
   ... ACKed       |  sent, not yet ACKed      |  usable, not yet sent   |  not usable ...
                   |<------ in flight -------->|                         |
                   |<------------- min(cwnd, rwnd) --------------------->|
```

`TcpSender._usable_window()` returns `min(cwnd, rwnd) − bytes_in_flight()`.
`TcpSender._send_allowed(now)` is the slide 3-125 action "transmit new segment(s), as allowed". It
loops:

1. Take `length = min(mss, bytes left to send, usable window)`. Stop if it is 0 or less.
2. If the segment would be shorter than one MSS while data is still in flight, and it is not the last
   piece of data, wait. ACKs will soon make room for a full segment. With nothing in flight, a short
   segment goes out anyway, so a window smaller than one MSS cannot stall us.
3. Send it, move `snd_nxt` forward, and start the retransmission timer if it is not running.
4. When all data is sent and `close()` was called, send the FIN.

So each feature has its own job in this one loop:

| Feature | What it controls in the loop |
| --- | --- |
| Reliable transfer | *which* bytes to send: new bytes, or old bytes again after a loss |
| Flow control | `rwnd`, so we never send more than the receiver has room for |
| Congestion control | `cwnd`, so we never send more than the network seems to carry |

A worked example with our numbers. MSS 1460, cwnd 4380 (3 segments), the client advertises
rwnd 65535, and nothing is in flight:

```text
usable = min(4380, 65535) - 0 = 4380  -> send 1460 bytes (in flight 1460)
usable = 4380 - 1460      = 2920      -> send 1460 bytes (in flight 2920)
usable = 4380 - 2920      = 1460      -> send 1460 bytes (in flight 4380)
usable = 4380 - 4380      = 0         -> stop; wait for an ACK
```

Here cwnd is the smaller cap, so congestion control decides. In the slow-reader demo (Part 3) rwnd is
2920 while cwnd is above 20000, so flow control decides. The Docker lab checks this rule on every
`SEND` and `RETRANSMIT` row of the trace: in flight never goes above min(cwnd, rwnd). The one-byte
zero-window probe is the only exception (Part 3).

### 1.6 Reading our log lines

Every TCP event prints one line in this shape:

```text
[Streap_tcp] <client_ip>:<client_port> <EVENT> key=value ... | cwnd=<n> ssthresh=<n> rwnd=<n> flight=<n> adv=<n>
```

The part after `|` is the state of the connection when the line was printed. `rwnd` is the client's
window. `adv` is the window we advertise to the client. The level is set with `STREAPROUS_TCP_LOG`:
0 prints only `DROP`, 1 (the default) prints the key events, and 2 also prints every `SEND`, `ACK`,
`RTT`, `RECV`, `APP_READ` and `ACK_SENT`.

Every event, whatever the level, is also one row in a CSV trace,
`out/trace_<client_ip>_<client_port>.csv`, with the columns
`time,event,cwnd,ssthresh,rwnd,in_flight,snd_una,snd_nxt,adv_wnd`. The CSV is where we look for the
level-2 detail after a demo.

In `server.log` the teacher's dispatcher thread prints at the same time as our worker. So a line can
end with `[Sock_dispatcher] Route packet ...` glued on. In the quotes below we cut that tail off and
change nothing else. The client port also changes on every run.

### 1.7 Running the tests

On Windows, from the repo root (no network and no root needed):

```sh
python -m unittest discover -s streaprous/tests -t streaprous
```

It prints `Ran <n> tests` and `OK`. To run one file only, add `-p "test_tcp_reno.py"`. The files that
matter for each pair:

| Pair | Test files |
| --- | --- |
| Reliable | `test_tcp_sender.py`, `test_tcp_receiver.py`, `test_tcp_rtt.py`, `test_tcp_segment.py`, `test_transfer_sim.py`, `test_tcp_connection.py` |
| Flow | `test_tcp_receiver.py` (window, update rule), `test_tcp_sender.py` (rwnd, probe), `test_transfer_sim.py` (`test_slow_reader`, `test_lost_window_update`) |
| Congestion | `test_tcp_reno.py` (every row of the slide 3-125 machine), `test_tcp_sender.py` |

`test_transfer_sim.py` runs whole transfers through `tests/sim.py`, a simulated lossy link in virtual
time, so a 200 KB transfer with losses takes milliseconds.

In Docker, with Docker Desktop running, this runs every scenario against a real Linux client and checks
each result:

```sh
python streaprous/docker/lab.py test
```

It prints `PASS <scenario>: <check>` lines and ends with `ALL PASSED`. `--only loss,trace` runs a
subset. The four demos are in Parts 2 to 4, and Appendix B lists the other commands.

---

## Part 2. Reliable data transfer

### 2.1 Code locations

| File | Functions |
| --- | --- |
| `tcp_sender.py` | `TcpSender.write`, `TcpSender.close`, `TcpSender.on_ack`, `TcpSender.on_tick`, `TcpSender._send_allowed`, `TcpSender._payload`, `TcpSender._segment_at_una`, `TcpSender._emit`, `TcpSender._start_timer`, `TcpSender._stop_timer`, `TcpSender._cancel_rtt_timing`, `TcpSender._take_rtt_sample`, `TcpSender._on_timeout`, `TcpSender._on_new_ack`, `TcpSender._on_duplicate_ack`, `TcpSender._fast_retransmit`, `TcpSender.bytes_in_flight`, `TcpSender.data_end`, `TcpSender.data_acked`, `TcpSender.fin_acked`, `TcpSender.next_deadline` |
| `tcp_rtt.py` | `RttEstimator.add_sample`, `RttEstimator.back_off` |
| `tcp_receiver.py` | `TcpReceiver.on_segment`, `TcpReceiver._deliver_in_order`, `TcpReceiver._store_out_of_order`, `TcpReceiver._pull_from_out_of_order`, `TcpReceiver.on_tick`, `TcpReceiver.read`, `TcpReceiver.readable`, `TcpReceiver.ack_sent`, `TcpReceiver.next_deadline` |
| `tcp_segment.py` | `parse_tcp_packet`, `parse_mss_option`, `wrap`, `unwrap`, `flag_names` |
| `tcp_connection.py` | `TcpConnection.process_packet`, `TcpConnection.process_timers`, `TcpConnection._transmit`, `TcpConnection._send_ack`, `TcpConnection._sendto`, `TcpConnection.send`, `TcpConnection.recv`, `TcpConnection.close`, `TcpConnection._worker`, `TcpConnection.start`, `TcpConnection._broken`, `TcpConnection._update_table`, `note_client_syn`, `get_connection`, `finish_connection` |
| `daemon/httpadapter.py` | `HttpAdapter.read_request_raw` (finds the end of a request in the byte stream) |

### 2.2 Slides it implements

- 3-77: "reliable, in-order byte stream", "cumulative ACKs", MSS.
- 3-78, 3-79: sequence numbers count bytes; the ACK is the "seq # of next byte expected".
- 3-80: an ACK can ride on a data segment (`Seq=79, ACK=43, data='C'`).
- 3-81 to 3-83: SampleRTT, EstimatedRTT, DevRTT, TimeoutInterval.
- 3-84: the simplified sender, with its three events (data from the application, timeout, ACK).
- 3-85: the receiver's four ACK rules.
- 3-86, 3-87: retransmission scenarios (lost ACK, premature timeout, a cumulative ACK covering a lost
  ACK).
- 3-88: fast retransmit.
- 3-96: the handshake agrees on the starting sequence numbers. We use the starting number of the
  client's SYN.

### 2.3 Mental model

The network can lose a segment, deliver it twice, or deliver it late and out of order. Reliable data
transfer turns that unreliable delivery into a perfect byte stream with two tools:

- **On the sender: keep a copy and a timer.** Keep every byte until it is ACKed. If no ACK comes in
  time, send it again.
- **On the receiver: put bytes in order and say what is missing.** Hand the application only bytes in
  order. Each ACK tells the sender the next byte we still need.

```text
sender                                   receiver
 keeps bytes snd_una .. snd_nxt           has every byte below rcv_nxt
 one timer for the oldest unACKed byte    buffers bytes that arrive above rcv_nxt
      --- seq=4921, 1460 bytes --->  X lost
      --- seq=6381, 1460 bytes --->       "gap!" -> ACK 4921 (duplicate)
      --- seq=7841, 1460 bytes --->       "gap!" -> ACK 4921 (duplicate)
 timer expires -> resend 4921  ---->      gap filled -> ACK 9301 (cumulative)
```

### 2.4 The terms, one at a time

- **Retransmission timer** (slide 3-84). One timer for the whole connection. "Think of timer as for
  oldest unACKed segment." `_start_timer` sets `timer_deadline = now + rto`. `_on_new_ack` restarts it
  while bytes are outstanding and stops it when everything is ACKed.
- **Timeout** (slides 3-84, 3-86). The timer expired: "retransmit segment that caused timeout, restart
  timer". In our code that is `_on_timeout`.
- **Duplicate ACK** (slide 3-85, row 3). An ACK that repeats the last ACK number. The receiver sends it
  when a segment arrives above the gap. The sender treats it as a duplicate only when data is
  outstanding, the segment carries no data and no FIN, and the window did not change (**Background**,
  RFC 5681). Without the last condition, a client that just announces a bigger window would look like
  a loss signal.
- **Fast retransmit** (slide 3-88). "If sender receives 3 additional ACKs for same data ('triple
  duplicate ACKs'), resend unACKed segment with smallest seq #." It is faster than waiting for the
  timer. In our code that is `_fast_retransmit`.
- **SampleRTT, EstimatedRTT, DevRTT, TimeoutInterval** (slides 3-81 to 3-83). How long to wait before
  a timeout. See 2.5.5.
- **`snd_una`, `snd_nxt`, `snd_max`** (sender). The oldest unACKed byte, the next byte to send, and one
  past the highest byte ever sent. `snd_max` is needed because `snd_nxt` can move *back* after a timeout
  (2.5.6).
- **`rcv_nxt`** (receiver). The next byte expected. It is also the ACK number we send.

### 2.5 The mechanism, traced on the real loss demo

The loss demo drops the 3rd and the 60th new data segment we send (`STREAPROUS_DROP_SEND=3,60`). Below
are the first rows of its saved CSV trace (`out/loss/trace_172.28.0.20_52864.csv`), captured with
`ssthresh=32768` before the default changed to 65536. Our first byte is 2001, MSS 1460, initial RTO 1 s.

```text
time,  event,   cwnd, ssthresh, rwnd,  in_flight, snd_una, snd_nxt
0.0202,SEND,    1460, 32768,    64240, 1460,      2001,    3461     segment 2001..3460
0.0214,RTT,     1460, 32768,    65535, 0,         3461,    3461     sample about 1.2 ms
0.0217,ACK,     2920, 32768,    65535, 0,         3461,    3461     ACK 3461
0.0220,SEND,    2920, 32768,    65535, 1460,      3461,    4921     segment 3461..4920
0.0223,SEND,    2920, 32768,    65535, 2920,      3461,    6381     segment 4921..6380
0.0234,DROP,    2920, 32768,    65535, 2920,      3461,    6381     ... and that one is dropped
0.0241,ACK,     4380, 32768,    65535, 1460,      4921,    6381     ACK 4921
0.0244,SEND,    4380, 32768,    65535, 2920,      4921,    7841     segment 6381..7840
0.0246,SEND,    4380, 32768,    65535, 4380,      4921,    9301     segment 7841..9300
0.0263,DUPACK,  4380, 32768,    65535, 4380,      4921,    9301     client: still want 4921 (1)
0.0268,DUPACK,  4380, 32768,    65535, 4380,      4921,    9301     client: still want 4921 (2)
0.2323,TIMEOUT, 1460, 2920,     65535, 1460,      4921,    6381     resend 4921..6380
0.2335,ACK,     2920, 2920,     65535, 0,         9301,    9301     ACK 9301 covers it all
0.2338,SEND,    2920, 2920,     65535, 1460,      9301,    10761    new data again
```

(We dropped the `adv_wnd` column, which stays 32768, and added the comments on the right.)

#### 2.5.1 The normal case: send, time, ACK

At 0.0202 the first segment goes out. `_send_allowed` starts the timer: deadline = 0.0202 + 1.0 s,
because no RTT is known yet and the initial RTO is 1 s. It also starts timing this segment for an RTT
sample (`timed_end = 3461`). At 0.0214 the ACK 3461 arrives:

- `on_ack` sees `3461 > snd_una`, so it calls `_on_new_ack`: `snd_una = 3461`.
- `_take_rtt_sample` measures 0.0012 s. `add_sample` gives EstimatedRTT 0.0012, DevRTT 0.0006 and
  TimeoutInterval 0.0012 + 4 × 0.0006 = 0.0036 s, which the floor raises to **0.2 s**.
- Nothing is outstanding, so the timer stops.

#### 2.5.2 A loss with a small window: the timeout

Segment 4921 is dropped. Segments 6381 and 7841 arrive at the client above the gap, so the client
sends two duplicate ACKs, "ACK 4921" (slide 3-85, row 3). Two are not three, so there is no fast
retransmit yet. And cwnd is 4380, three segments, all in flight, so `_send_allowed` sends nothing more.
No more segments go out, so no third duplicate ACK can come.

So the timer saves us. The ACK at 0.0241 restarted it with RTO 0.2 s, and the worker, which checks
timers every 10 ms, finds it expired at 0.2323. `_on_timeout`:

1. `rtt.back_off()`: RTO 0.2 → 0.4 s (slide 3-84 doubling; the log prints the new value `rto=0.400`).
2. `cc.on_timeout()`: cwnd → 1460, ssthresh → 2920 (Part 4).
3. Cancels RTT timing (slide 3-81: "ignore retransmissions").
4. `snd_nxt = snd_una = 4921` (go back), then resends one segment at 4921 and restarts the timer.

The client now has every byte up to 9300 (it had kept 6381..9300 in its buffer), so it answers
**ACK 9301**. ACKs are cumulative (slide 3-87). `_on_new_ack` sets `snd_una = 9301` and
`snd_nxt = max(6381, 9301) = 9301`, so we never resend 6381 or 7841. That is why the demo prints
`RETRANSMIT x0`.

#### 2.5.3 A loss with a big window: fast retransmit

The 60th segment (seq 88141) is dropped when cwnd is 14600, ten segments. The nine segments sent
after it arrive above the gap, so the client sends nine duplicate ACKs. On the third one,
`RenoCongestion.on_duplicate_ack` returns True, and `_on_duplicate_ack` calls `_fast_retransmit`. It
resends the segment at `snd_una` at once, without waiting for the timer. `snd_nxt` and the timer stay
as they are. The real lines:

```text
[Streap_tcp] 172.28.0.20:52864 DUPACK ack=88141 count=2 | cwnd=15726 ssthresh=2920 rwnd=65535 flight=14600 adv=32768
[Streap_tcp] 172.28.0.20:52864 STATE old=CONGESTION_AVOIDANCE new=FAST_RECOVERY | cwnd=12243 ssthresh=7863 rwnd=65535 flight=14600 adv=32768
[Streap_tcp] 172.28.0.20:52864 DUPACK ack=88141 count=3 | cwnd=12243 ssthresh=7863 rwnd=65535 flight=14600 adv=32768
[Streap_tcp] 172.28.0.20:52864 FAST_RETRANSMIT seq=88141 len=1460 | cwnd=12243 ssthresh=7863 rwnd=65535 flight=14600 adv=32768
```

`STATE` prints before `DUPACK count=3`, because Reno changes state inside the call that counts the
third duplicate ACK.

Compare the two cases. Timeout: cwnd was 3 segments, so there were too few later segments to make 3
duplicate ACKs, and we waited 0.2 s. Fast retransmit: cwnd was 10 segments, there were enough
duplicate ACKs, and the resend went out the moment the third one arrived, with no wait for the timer.

#### 2.5.4 The receiver side: four ACK rules

`TcpReceiver.on_segment(seq, payload, fin, now)` decides what to do with each data segment and returns
the reason for an ACK, or None. The rows are those of slide 3-85:

| Slide 3-85 row | When | We do | Return value |
| --- | --- | --- | --- |
| 1 | in-order segment, no ACK pending | deliver; start the delayed-ACK timer (0.1 s) | `None`, later `'delayed'` from `on_tick` |
| 2 | in-order segment, an ACK already pending | deliver; ACK both at once | `'cumulative'` |
| 3 | segment above `rcv_nxt` (a gap) | keep it in `out_of_order`; send a duplicate ACK at once | `'duplicate'` |
| 4 | segment that fills (part of) the gap | deliver it and the stored segments that now fit; ACK at once | `'gap_filled'` |

Three more cases are not rows on the slide but follow from it:

- All the bytes were received before: event `DUP_SEGMENT`, ACK at once (`'old_segment'`). This is the
  premature-timeout case of slide 3-86, where B answers a resent segment with a cumulative ACK.
- A segment reaches past the window's right edge: the extra part is cut off, event `OUT_OF_WINDOW`
  (Part 3).
- The FIN arrives in order: `rcv_nxt` moves one past it, event `FIN_RECEIVED`, ACK at once (`'fin'`).

`_deliver_in_order` appends bytes to `ready` and moves `rcv_nxt`. `_store_out_of_order` keeps a segment
above the gap. If two segments start at the same number, the longer one is kept.
`_pull_from_out_of_order` moves stored segments into `ready` once they touch `rcv_nxt`. It drops
segments already covered and cuts overlaps, so every byte is delivered exactly once.

A tiny trace. `rcv_nxt` = 1000 (the client's numbers are large, so we use small ones here), and the
client sends 536-byte segments:

```text
seq 1000 len 536 arrives -> row 1: deliver, rcv_nxt 1536, start 0.1 s delayed-ACK timer
seq 2072 len 536 arrives -> row 3: store, gap 1536..2071, send ACK 1536 at once (duplicate)
seq 1536 len 536 arrives -> row 4: deliver it + pull 2072 from the store, rcv_nxt 2608, ACK 2608
seq 1536 len 536 again   -> already have it: DUP_SEGMENT, ACK 2608
```

`TcpConnection._send_ack(reason, now)` builds the pure ACK: `ack = rcv_nxt`, `window = window()`.
Every data segment we send also carries `ack = rcv_nxt` (piggybacking, slide 3-80), and
`TcpReceiver.ack_sent` then clears the pending delayed ACK.

Real lines from the `upload` scenario. Linux uploads 300 000 bytes, and netem on the client reorders,
duplicates and drops some of its segments:

```text
[Streap_tcp] 172.28.0.20:49792 OUT_OF_ORDER seq=3664248590 len=1072 expected=3664247518 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=30624
[Streap_tcp] 172.28.0.20:49792 DUP_ACK_SENT ack=3664247518 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=29552
[Streap_tcp] 172.28.0.20:49792 DUP_SEGMENT seq=3664261990 len=1072 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=29552
[HttpAdapter] Read request: header 150 bytes, body 300000 bytes, sha256 5dd581b085273e13cf72eb568ae2a75b50e94b7d3cb7e0498638f6a0b87a7801
```

The sha256 equals the client's `sent_sha256`, so every byte arrived in order. The lengths are 1072 =
2 × 536 because Docker's virtual network merges two client segments into one packet before our raw
socket reads it. The client sends 536-byte pieces because our SYN-ACK carries no MSS option
(**Background**: then 536 is the default).

#### 2.5.5 How long to wait: RTT and the timeout

Too short a timeout resends segments that were only slow. Too long a timeout reacts slowly to a loss
(slide 3-81). So `RttEstimator.add_sample` follows the measured round trip time:

```text
EstimatedRTT    = (1 - 0.125) * EstimatedRTT + 0.125 * SampleRTT              (slide 3-82)
DevRTT          = (1 - 0.25)  * DevRTT + 0.25 * |SampleRTT - EstimatedRTT|    (slide 3-83)
TimeoutInterval = EstimatedRTT + 4 * DevRTT                                   (slide 3-83)
```

What each symbol means. EstimatedRTT is a smoothed average: each new sample moves it only 1/8 of the
way. DevRTT is the average distance of a sample from that average. A jumpy network gets a big DevRTT,
so it gets a larger safety margin. A steady network gets a timeout just above its RTT.

An example (these exact numbers are in `test_tcp_rtt.py`). Before any sample, RTO = 1.0 s.

```text
sample 0.1 s (first): EstimatedRTT 0.1,    DevRTT 0.05,   timeout 0.1 + 0.2 = 0.3 s
sample 0.2 s:         DevRTT 0.75*0.05 + 0.25*|0.2-0.1| = 0.0625
                      EstimatedRTT 0.875*0.1 + 0.125*0.2 = 0.1125
                      timeout 0.1125 + 4*0.0625 = 0.3625 s
timeout, timeout, ... back_off: 0.725, 1.45, 2.9, 4.0 (capped)
```

Rules from outside the slides (**Background**): the first sample sets EstimatedRTT = sample and
DevRTT = sample / 2 (RFC 6298). DevRTT is updated with the old EstimatedRTT, as the slide 3-83 formula
reads. The timeout stays between a 0.2 s floor and a 4 s ceiling. In Docker the RTT is about 1 ms, so
the floor decides, and the loss demo's `TIMEOUT ... rto=0.400` is the 0.2 s floor doubled once.

`_take_rtt_sample` times one segment at a time and only a first transmission. After a retransmission,
an ACK cannot tell which copy it answers, so every retransmission cancels the timing (slide 3-81:
"ignore retransmissions").

#### 2.5.6 Edge cases the teacher may ask about

- **Go back after a timeout** (**Background**, BSD Reno). `_on_timeout` sets `snd_nxt = snd_una`. Then
  `_send_allowed` resends the rest of the old window as cwnd allows, with kind `'retransmit'`. Several
  losses in one window then cost one timeout, not one timeout each. The trace demo shows it after a
  burst of 31 drops: `TIMEOUT seq=584541`, then `RETRANSMIT seq=586001`, `RETRANSMIT seq=587461`, and
  two more for each new ACK as slow start grows cwnd.
- **A resent segment longer than its first copy.** A short segment can go out when the window was
  smaller than one MSS. `_on_timeout` and `_fast_retransmit` resend a full MSS from `snd_una` when the
  data allows. That copy can reach past `snd_max`, so both move `snd_max` forward with it. Without
  that, the client's ACK for the whole resend would be above `snd_max`, and `on_ack` would ignore it as
  impossible (`test_timeout_resend_longer_than_first_copy`).
- **Old and impossible ACKs.** `on_ack` ignores an ACK below `snd_una` (old) and above `snd_max` (we
  never sent that byte). The one exception is a zero-window probe byte (Part 3).
- **The sequence field of a pure ACK.** `_send_ack` puts `snd_max`, not `snd_nxt`, in the sequence
  field. After a go-back, `snd_nxt` is lower than bytes the client already has. An ACK carrying that
  lower number would look like an old segment to the client, which could then ignore our ACK and window
  (**Background**, BSD `tcp_output`).
- **The first expected client byte.** `TcpConnection` takes `irs` (the first client byte we expect)
  from the SYN's sequence number + 1, which `note_client_syn` stores during `accept`. The teacher's
  `accept` takes `client_seq` from the third handshake packet. But the client can send data right after
  its ACK, and a reordered data segment can arrive *before* that ACK; its sequence number would then be
  wrong. In the loss demo: `Handshake1: Received SYN from Client (172.28.0.20:52864) (Seq=26912282)`
  and then `OPEN ... irs=26912283`.
- **Wrap-around.** The header has 32 bits, so sequence numbers wrap at 2^32 (slide 3-79 shows the
  sequence space). Inside our code they are plain unbounded integers. `wrap` turns them into 32 bits
  for the wire. `unwrap(value, near)` picks the integer with those 32 bits that is closest to `near`
  (`rcv_nxt` or `snd_una`). The closest-integer rule is **Background**.
- **The FIN.** `TcpSender.close` asks for a FIN after the last data byte. It takes one sequence number
  and the same timer resends it until it is ACKed. In the loss demo, `FIN_SENT seq=1050758` = 2001 +
  1 048 757 response bytes. `TcpConnection.close` then waits up to 2 s for the client's FIN and ACKs
  it. The teacher's code dropped that FIN.
- **Giving up.** After 8 timeouts in a row, or 60 s with no packet from the client while we wait for
  it, the event `GIVE_UP` fires, and `send` and `recv` return their failure values (**Background**:
  our own limits).
- **Finding the end of a request.** TCP gives a byte stream (handout §2.1, "stream chunks"), so
  `HttpAdapter.read_request_raw` calls `run_conn_recv` until it sees `\r\n\r\n`, then until it has
  `Content-Length` body bytes. The `http` scenario sends a 3000-byte header and a 150 000-byte body to
  test it.

### 2.6 Why it exists: what breaks without it

- Without a timer, one lost segment stops the download forever. The client waits for byte 4921, and we
  never send it again. The teacher's 5 s sleep only hid this.
- Without the receiver's ordering, an out-of-order segment would go straight into the HTTP request,
  and the upload's sha256 would not match.
- Without fast retransmit, every loss would cost at least 0.2 s of silence, even with plenty of
  duplicate ACKs telling us exactly what is missing.
- Without the RTT estimate, a fixed timeout is either too short on a slow path (needless resends) or
  too long on a fast one (slow recovery).

### 2.7 Demo

```sh
python streaprous/docker/lab.py demo loss
```

What it does: the server runs with `STREAPROUS_DROP_SEND=3,60`, and a Linux client downloads
`demo_1m.bin` (1 048 576 bytes) and checks its sha256. The demo prints the client's `RESULT` line, up
to three server lines for each of `DROP`, `TIMEOUT`, `DUPACK`, `FAST_RETRANSMIT` and `RETRANSMIT`, and
the output files in `streaprous/out/loss/`. The lines to point at (from `out/loss/server.log`):

```text
[Streap_tcp] 172.28.0.20:52864 DROP dir=send why=nth=3 seq=4921 len=1460 | cwnd=2920 ssthresh=32768 rwnd=65535 flight=2920 adv=32768
[Streap_tcp] 172.28.0.20:52864 DUPACK ack=4921 count=1 | cwnd=4380 ssthresh=32768 rwnd=65535 flight=4380 adv=32768
[Streap_tcp] 172.28.0.20:52864 DUPACK ack=4921 count=2 | cwnd=4380 ssthresh=32768 rwnd=65535 flight=4380 adv=32768
[Streap_tcp] 172.28.0.20:52864 TIMEOUT seq=4921 len=1460 rto=0.400 | cwnd=1460 ssthresh=2920 rwnd=65535 flight=1460 adv=32768
[Streap_tcp] 172.28.0.20:52864 DROP dir=send why=nth=60 seq=88141 len=1460 | cwnd=14600 ssthresh=2920 rwnd=65535 flight=14600 adv=32768
[Streap_tcp] 172.28.0.20:52864 FAST_RETRANSMIT seq=88141 len=1460 | cwnd=12243 ssthresh=7863 rwnd=65535 flight=14600 adv=32768
[Streap_tcp] 172.28.0.20:52864 CLOSE sent=722 retransmits=2 timeouts=1 fast_retransmits=1 probes=0 received=1 seconds=1.048 | cwnd=53083 ssthresh=7863 rwnd=56972 flight=0 adv=32768
```

and the client's result (`out/loss/client.txt`):

```text
RESULT method=GET path=/demo_1m.bin status=200 body_bytes=1048576 sha256=3dbbefab3a76b40176de21b69bb7555e09c72c4714199395c82d9a746446dcb0 expect=ok handshake=ok seconds=1.185
```

How to explain it: two segments were lost, we resent exactly two (`retransmits=2`), one by timeout and
one by fast retransmit, and the file arrived whole (`expect=ok`). For the receiver side, run
`python streaprous/docker/lab.py test --only upload` and point at the `OUT_OF_ORDER`, `DUP_ACK_SENT`
and `DUP_SEGMENT` lines in `out/upload/server.log` (2.5.4).

### 2.8 Self-check questions

1. In the loss demo, why did the first loss end in a timeout, not a fast retransmit?
   *Answer:* cwnd was 4380, three segments. After the lost one, only two segments reached the client,
   so only two duplicate ACKs came back, and cwnd allowed nothing more to be sent. Three are needed for
   a fast retransmit.
2. After the timeout we resent 4921 and got ACK 9301. Why did we not resend 6381 and 7841?
   *Answer:* ACKs are cumulative (slide 3-87). ACK 9301 says the client has every byte below 9301, so
   `_on_new_ack` moves `snd_una` and `snd_nxt` to 9301.
3. Why do we take no RTT sample from a retransmitted segment?
   *Answer:* the ACK could answer either copy, so the sample could be far too small or too large.
   Slide 3-81 says "ignore retransmissions". `_take_rtt_sample` only times first transmissions, and
   every retransmission cancels the timing.

---

## Part 3. Flow control

### 3.1 Code locations

| File | Functions |
| --- | --- |
| `tcp_receiver.py` | `TcpReceiver.window` (the advertised window), `TcpReceiver.window_update_due`, `TcpReceiver.read`, `TcpReceiver.ack_sent`, `TcpReceiver.on_segment` (cuts data past the right edge) |
| `tcp_sender.py` | `TcpSender._usable_window`, `TcpSender._update_rwnd`, `TcpSender._probe_needed`, `TcpSender._check_persist`, `TcpSender._send_window_probe`, `TcpSender.on_ack` (accepts an ACK of the probe byte), `TcpSender.on_tick` |
| `tcp_connection.py` | `TcpConnection.recv` (sends the window update), `TcpConnection._send_ack`, `TcpConnection._transmit` (window field on every segment), `TcpConnection._note_advertised` |
| `streaprous_raw_connection.py` | `build_tcp_packet` and `send_tcp_data_segment` get a `window` argument; its default is `RECV_BUFFER` from `tcp_config.py` |

### 3.2 Slides it implements

- 3-90 to 3-93: the problem. "What happens if network layer delivers data faster than application
  layer removes data from socket buffers?" The answer: "receiver controls sender, so sender won't
  overflow receiver's buffer by transmitting too much, too fast".
- 3-94: the receiver "advertises" free buffer space in the rwnd field; the "sender limits amount of
  unACKed ('in-flight') data to received rwnd"; this "guarantees receive buffer will not overflow".
- 3-95: the receive window field in the TCP header.
- 3-79: the window drawn in the sender's sequence space.

### 3.3 Mental model

The receiver has a bucket of fixed size (RcvBuffer). TCP pours arriving bytes in, and the application
scoops them out with `read`. Every ACK says how much empty space is left. The sender never has more
bytes in flight than that space, so the bucket cannot overflow.

```text
              RcvBuffer = 32768 bytes
 +-------------------------------+----------------------+
 |  in-order bytes not yet read  |   free space = rwnd  |  <- the advertised window
 +-------------------------------+----------------------+
    ^ the application reads here    ^ new segments land here
```

Flow control works in both directions in our server:

- **We are the receiver** when a client uploads (POST). `TcpReceiver.window()` is the number we
  advertise.
- **We are the sender** when we answer (GET). The client's window arrives in every ACK and becomes our
  `rwnd`.

### 3.4 The terms, one at a time

- **RcvBuffer** (slide 3-94). Our receive buffer: 32768 bytes (`STREAPROUS_RCVBUF`, 1 to 65535). It
  must stay at most 65535, because the window field has 16 bits and we do not use window scaling.
- **rwnd, the advertised window** (slides 3-94, 3-95). Our code: `RcvBuffer − (LastByteRcvd −
  LastByteRead)`, that is, `buffer_size − len(ready)`.
- **Zero window.** rwnd = 0: the buffer is full, so stop sending.
- **Window update.** An ACK sent only to tell the sender that the window opened again.
- **Zero-window probe and persist timer** (**Background**). When rwnd is 0 and nothing is in flight,
  no ACK will ever come to tell us the window opened. So a timer asks, by sending one byte.

### 3.5 The mechanism, traced with our numbers

#### 3.5.1 As receiver: computing the window

`TcpReceiver.window()` returns `buffer_size − (last_byte_rcvd − last_byte_read)`, kept between 0 and
`buffer_size`. With RcvBuffer = 32768:

```text
start                                       unread 0      window 32768
3 in-order segments of 1460 arrive          unread 4380   window 28388
the application reads 2048 bytes            unread 2332   window 30436
the application stops reading; the client
fills the buffer                            unread 32768  window 0      -> WINDOW_ZERO
```

This is the surprising part: bytes waiting in `out_of_order` are **not** subtracted. They lie inside
the space we already advertised, between `rcv_nxt` and `rcv_nxt + window()`, and `on_segment` cuts
anything past that right edge (`OUT_OF_WINDOW`). So the memory we use stays at or below RcvBuffer
anyway.

Every segment we send carries `window()` in its window field (`_transmit`, `_send_ack`), using the
`window` argument we added to the teacher's builders. `_note_advertised` logs `WINDOW_ZERO` when the
advertised value changes from above 0 to 0.

#### 3.5.2 As receiver: when to send a window update

After the application reads, `TcpConnection.recv` asks `TcpReceiver.window_update_due()`:

```text
last_advertised < RcvBuffer / 4 <= window()        (RcvBuffer / 4 = 8192 for 32768)
```

If it is true, `_send_ack('window_update', now)` goes out at once. This is our Lab 2 design rule F4
(**Background**). Traced:

```text
we advertised 0 (buffer full)
read 4096 -> window 4096; 4096 < 8192, no update yet
read 4096 -> window 8192; last advertised 0 < 8192 <= 8192 -> WINDOW_UPDATE window=8192
```

Why wait for a quarter? Each read frees only a little space. An extra ACK after every small read adds
packets that carry little news, and a sender told about a tiny window may answer with tiny segments
(silly window syndrome, **Background**). Waiting until a quarter of the buffer is free makes each
update worth sending. Why send one at all? A sender facing a zero window would otherwise wait for its
own probe timer.

#### 3.5.3 As sender: obeying the client's window

Every ACK carries the client's window. `on_ack` hands it to `_update_rwnd`, which logs `RWND_ZERO`
when it drops to 0 and `RWND_OPEN` when it opens again. `_usable_window()` uses
`min(cwnd, rwnd) − in flight` (1.5), so a small rwnd caps us however large cwnd is.

When rwnd is 0, nothing is in flight and data is waiting (`_probe_needed`), `_check_persist` arms the
persist timer. When it expires, `_send_window_probe` sends **one byte** at `snd_nxt`:

- The byte is not counted in flight, and `snd_nxt` does not move.
- The client must answer it with an ACK that carries its current window. So even if the client's
  window update was lost, we learn when the window opens.
- If the client keeps the byte and ACKs `snd_nxt + 1`, `on_ack` accepts that ACK (its upper limit is
  `probe_seq + 1`).
- The interval starts at 0.2 s and doubles up to 2 s. `RWND_OPEN` resets it to 0.2 s. This is our Lab
  2 design rule F3 (**Background**).

Traced on the real slow-reader run (MSS 1460, RTO and ssthresh at their defaults):

```text
t=0.70  ACK 39961 with window 0  -> RWND_ZERO; nothing in flight; persist timer 0.2 s
t=0.90  PROBE seq=39961 (1 byte)  -> client: window 0 still; next interval 0.4 s
t=1.31  PROBE seq=39961           -> window 0;  next 0.8 s
t=2.11  PROBE seq=39961           -> window 0;  next 1.6 s
t=2.63  client's own update: 2920 -> RWND_OPEN; interval back to 0.2 s; SEND 39961.. resumes
```

### 3.6 Why it exists: what breaks without it

- Without rwnd on the sender, a fast server floods a slow client. The client's kernel has to drop
  what does not fit, and every dropped byte costs a retransmission, sent into a buffer that is still
  full.
- Without our window on the receiver side, the dispatcher's queue (100 packets) could overflow while
  our application is slow. With RcvBuffer 32768 and 536-byte client segments, at most 61 segments fit
  in the window, so the queue never drops data the window allowed.
- Without the probe, a lost window update leaves both sides waiting for each other forever: the
  sender waits for an ACK, and the receiver has nothing to ACK. (`test_lost_window_update` builds
  exactly this case.)

### 3.7 Demos

**We are the sender: the slow reader.**

```sh
python streaprous/docker/lab.py demo slow-reader
```

The Linux client downloads `demo_256k.bin`. It asks for a 4096-byte socket buffer, reads 4096 bytes
every 0.05 s, and pauses 2 s after 32768 bytes. Its windows stay small (`OPEN ... rwnd=2920`). Point at these lines (`out/slow-reader/server.log`):

```text
[Streap_tcp] 172.28.0.20:50478 RWND_ZERO ack=39961 | cwnd=20440 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:50478 PROBE seq=39961 interval=0.200 | cwnd=21900 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:50478 PROBE seq=39961 interval=0.400 | cwnd=21900 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:50478 PROBE seq=39961 interval=0.800 | cwnd=21900 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:50478 RWND_OPEN rwnd=2920 | cwnd=21900 ssthresh=32768 rwnd=2920 flight=0 adv=32768
```

Read them aloud: cwnd is 21900, yet `flight=0`. The network would allow 15 segments, but the receiver
allows none, so flow control wins `min(cwnd, rwnd)`. Right after `RWND_OPEN` the teacher's builder
prints `[Streap_raw] send_tcp_data_segment: seq=39961, ack_seq=3802599222, payload_len=1460,
flags=0x18`, so the transfer continues from the probed byte. The download still ends `expect=ok`.

**We are the receiver: the slow application.**

```sh
python streaprous/docker/lab.py demo slow-app
```

The server runs with `STREAPROUS_RCVBUF=16384`, `STREAPROUS_APP_READ_DELAY=0.05` and
`STREAPROUS_APP_READ_BYTES=2048`: our HTTP code reads 2048 bytes every 0.05 s while Linux uploads
300 000 bytes. A quarter of 16384 is 4096. The lines (`out/slow-app/server.log`):

```text
[Streap_tcp] 172.28.0.20:52546 WINDOW_ZERO ack=3208492850 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=0
[Streap_tcp] 172.28.0.20:52546 WINDOW_UPDATE window=4096 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=4096
[HttpAdapter] Read request: header 150 bytes, body 300000 bytes, sha256 5dd581b085273e13cf72eb568ae2a75b50e94b7d3cb7e0498638f6a0b87a7801
```

Between those two lines, the CSV trace (`out/slow-app/trace_172.28.0.20_52546.csv`) shows the
application reading twice: `6.5402,APP_READ,...,0` and `6.5911,APP_READ,...,0`. The last column,
`adv_wnd`, is the window we last *sent*, so it stays 0 until a segment goes out. The free space grows
0 → 2048 → 4096, and only at 4096 does the update go out (`6.5928,WINDOW_UPDATE,...,4096`). The demo
also draws `out/slow-app/window.svg`, our advertised window over time. The checks confirm that a
`win 0` segment is in the packet capture.

### 3.8 Self-check questions

1. RcvBuffer is 32768, 20000 in-order bytes are unread, and 3000 bytes sit in `out_of_order`. What
   window do we advertise?
   *Answer:* 32768 − 20000 = 12768. Out-of-order bytes are not subtracted, because they lie inside the
   space we already advertised.
2. The client's window is 0 and its window update gets lost. Why does our download not hang?
   *Answer:* nothing is in flight and data is waiting, so the persist timer sends a one-byte probe
   (0.2 s, then 0.4, 0.8, ... up to 2 s). The client must answer it with its current window.
3. Our window was 0, and the application reads 4096 bytes with RcvBuffer 32768. Do we send a window
   update?
   *Answer:* no. The window is 4096, below 8192 (a quarter). We send one once the window reaches 8192.
   Until then the client's own probes get our current window in reply.

---

## Part 4. Congestion control (TCP Reno)

### 4.1 Code locations

| File | Functions |
| --- | --- |
| `tcp_reno.py` | `RenoCongestion.on_new_ack`, `RenoCongestion.on_duplicate_ack`, `RenoCongestion.on_timeout`, `RenoCongestion._set_state`, `RenoCongestion._halve_ssthresh` |
| `tcp_sender.py` | `TcpSender._usable_window` and `TcpSender._send_allowed` (use cwnd), `TcpSender._on_new_ack` (calls `cc.on_new_ack`), `TcpSender._on_duplicate_ack` (calls `cc.on_duplicate_ack`), `TcpSender._fast_retransmit`, `TcpSender._on_timeout` (calls `cc.on_timeout`) |
| `tcp_config.py` | `initial_ssthresh` (`STREAPROUS_SSTHRESH`, default 65536) |
| `tools/plot_trace.py` | draws cwnd, ssthresh and rwnd from the CSV trace |

### 4.2 Slides it implements

- 3-120: AIMD. "Increase sending rate by 1 maximum segment size (MSS) every RTT until loss detected";
  "cut sending rate in half at each loss event".
- 3-121: "cut in half on loss detected by triple duplicate ACK (TCP Reno)"; "cut to 1 MSS when loss
  detected by timeout".
- 3-122: "LastByteSent − LastByteAcked ≤ cwnd"; "TCP rate ≈ cwnd / RTT bytes/sec".
- 3-123: slow start. "Initially cwnd = 1 MSS", "double cwnd every RTT", "done by incrementing cwnd for
  every ACK received".
- 3-124: the variable ssthresh. "On loss event, ssthresh is set to 1/2 of cwnd just before loss event".
- 3-125: the full state machine, which `RenoCongestion` follows row by row.
- 3-88: fast retransmit, which starts fast recovery.

### 4.3 Mental model

Flow control protects the receiver. Congestion control protects the **network** between us, which is
shared with everyone else. Nobody tells the sender how much the network can carry, so the sender
probes. It speeds up while ACKs come back, and it slows down when a loss shows the network is full.
The result is the AIMD sawtooth of slide 3-120:

```text
cwnd
 ^           /|            /|
 |          / |           / |        additive increase: +1 MSS per RTT
 |         /  |          /  |        multiplicative decrease: halve on loss
 |    /|  /   |    /|   /   |
 |   / | /    |   / |  /    |
 |  /  |/     |  /  | /     |
 +---------------------------------> time
```

Because the rate is about `cwnd / RTT` (slide 3-122), changing cwnd changes how fast we send.

### 4.4 The terms, one at a time

- **cwnd** (slide 3-122). The congestion window in bytes. It starts at 1 MSS = 1460 (slide 3-123).
- **ssthresh** (slide 3-124). The slow start threshold: below it cwnd grows fast, above it slowly. It
  starts at 65536 bytes (64 KB, as shown on slide 3-125); `STREAPROUS_SSTHRESH` can override it for a
  demo.
- **Slow start** (slide 3-123). +1 MSS per new ACK, so cwnd doubles every RTT. "Slow" means it starts
  small, not that it grows slowly.
- **Congestion avoidance** (slide 3-125). +MSS·(MSS/cwnd) per new ACK, about +1 MSS per RTT. In integer
  bytes: `max(1, mss * mss // cwnd)`.
- **Fast recovery** (slide 3-125). After a triple duplicate ACK: halve, then add 3 MSS for the three
  segments that already left the network, and +1 MSS for each further duplicate ACK.
- **dupACKcount** (slide 3-125). `dup_ack_count`: duplicate ACKs in a row.

### 4.5 The mechanism, traced with our numbers

`RenoCongestion` holds `cwnd`, `ssthresh`, `dup_ack_count` and `state`. The sender calls exactly one
of its three methods for each event:

| State | Event | Action (slide 3-125) | Our method |
| --- | --- | --- | --- |
| SLOW_START | new ACK | `cwnd += MSS`; if `cwnd > ssthresh`: → CONGESTION_AVOIDANCE | `on_new_ack` |
| CONGESTION_AVOIDANCE | new ACK | `cwnd += max(1, MSS*MSS // cwnd)` | `on_new_ack` |
| FAST_RECOVERY | new ACK | `cwnd = ssthresh`; → CONGESTION_AVOIDANCE | `on_new_ack` |
| SLOW_START, CONGESTION_AVOIDANCE | duplicate ACK | `dup_ack_count += 1`; at 3: `ssthresh = cwnd // 2`, `cwnd = ssthresh + 3*MSS`, → FAST_RECOVERY, retransmit | `on_duplicate_ack` (returns True) |
| FAST_RECOVERY | duplicate ACK | `cwnd += MSS` | `on_duplicate_ack` (returns False) |
| any | timeout | `ssthresh = cwnd // 2`, `cwnd = MSS`, `dup_ack_count = 0`, → SLOW_START | `on_timeout` |

Every new ACK also sets `dup_ack_count = 0`. `_halve_ssthresh` never lets ssthresh go below 2 MSS
(**Background**, RFC 5681). `_set_state` logs a `STATE` line on every change.

**Slow start** (MSS 1460, ssthresh 65536). One new ACK at a time:

```text
cwnd 1460 -> 2920 -> 4380 -> ... -> 64240 (44 MSS, still <= 65536: slow start)
     -> 65700 (45 MSS > 65536) -> STATE old=SLOW_START new=CONGESTION_AVOIDANCE
```

The following saved trace snippets in sections 4.5-4.8 were captured with the previous default,
`ssthresh=32768`; their runtime values are historical, not output from the current default. Re-run demo c
before presenting current demo numbers, or set `STREAPROUS_SSTHRESH=32768` to reproduce the old run.
The real line from that earlier trace:

```text
[Streap_tcp] 172.28.0.20:40434 STATE old=SLOW_START new=CONGESTION_AVOIDANCE | cwnd=33580 ssthresh=32768 rwnd=65535 flight=30660 adv=32768
```

A cwnd equal to ssthresh stays in slow start, because the slide 3-125 arrow says `cwnd > ssthresh`.

**Congestion avoidance.** 1460 × 1460 = 2 131 600:

```text
33580 + 2131600 // 33580 (= 63) = 33643
33643 + 63 = 33706,  then 33769, 33832, ...   about +1460 per RTT once a whole window is ACKed
```

**Triple duplicate ACK** (real numbers from the trace demo). cwnd is 40800 when the third duplicate
ACK arrives:

```text
ssthresh = 40800 // 2 = 20400
cwnd     = 20400 + 3*1460 = 24780        -> FAST_RECOVERY, fast retransmit seq=219541
each further duplicate ACK: cwnd 26240, 27700, ... (+1460)
new ACK: cwnd = ssthresh = 20400         -> CONGESTION_AVOIDANCE
```

```text
[Streap_tcp] 172.28.0.20:40434 DUPACK ack=219541 count=2 | cwnd=40800 ssthresh=32768 rwnd=65535 flight=39420 adv=32768
[Streap_tcp] 172.28.0.20:40434 STATE old=CONGESTION_AVOIDANCE new=FAST_RECOVERY | cwnd=24780 ssthresh=20400 rwnd=65535 flight=39420 adv=32768
[Streap_tcp] 172.28.0.20:40434 FAST_RETRANSMIT seq=219541 len=1460 | cwnd=24780 ssthresh=20400 rwnd=65535 flight=39420 adv=32768
[Streap_tcp] 172.28.0.20:40434 STATE old=FAST_RECOVERY new=CONGESTION_AVOIDANCE | cwnd=20400 ssthresh=20400 rwnd=65535 flight=17520 adv=32768
```

Why does cwnd grow during fast recovery? Each duplicate ACK means one more segment has left the
network: it arrived at the client, just above the gap. So one more segment may go in. While cwnd is
below `flight=39420`, nothing new goes out. Once cwnd is more than one MSS above it,
`_on_duplicate_ack` calls `_send_allowed`, which sends new data. In the log, `flight` starts to rise:
`cwnd=43760 ... flight=40880`, then `cwnd=45220 ... flight=42340`. That is the slide's "transmit new
segment(s), as allowed" in fast recovery. In fast recovery the
`DUPACK` lines keep `count=3`, because the counter only matters until the third.

**Timeout** (real numbers from the trace demo). cwnd 36902 when the timer expires:

```text
ssthresh = 36902 // 2 = 18451, cwnd = 1460 -> SLOW_START
```

```text
[Streap_tcp] 172.28.0.20:40434 STATE old=CONGESTION_AVOIDANCE new=SLOW_START | cwnd=1460 ssthresh=18451 rwnd=65535 flight=36500 adv=32768
[Streap_tcp] 172.28.0.20:40434 TIMEOUT seq=584541 len=1460 rto=0.400 | cwnd=1460 ssthresh=18451 rwnd=65535 flight=1460 adv=32768
```

`STATE` prints before `TIMEOUT` because `_on_timeout` calls `cc.on_timeout()` before it logs.

**The ssthresh floor** (real numbers from the loss demo). The timeout came at cwnd 4380, and
4380 // 2 = 2190 is below 2 MSS = 2920, so ssthresh = 2920:

```text
[Streap_tcp] 172.28.0.20:52864 TIMEOUT seq=4921 len=1460 rto=0.400 | cwnd=1460 ssthresh=2920 rwnd=65535 flight=1460 adv=32768
[Streap_tcp] 172.28.0.20:52864 STATE old=SLOW_START new=CONGESTION_AVOIDANCE | cwnd=4380 ssthresh=2920 rwnd=65535 flight=1460 adv=32768
```

Then cwnd grows 1460 → 2920 (equal, still slow start) → 4380 (> 2920, congestion avoidance) → 4866 →
5304 → 5705. Each step is `+ 2131600 // cwnd`; the CSV of the loss demo shows exactly these values.

Timeout vs triple duplicate ACK: this is easy to confuse. Both are loss signals, but they mean
different things. Three duplicate ACKs mean later segments still arrive, so the network still works,
and we only halve (slide 3-121, Reno). A timeout means nothing came back at all, so we start over from
1 MSS.

### 4.6 Why it exists: what breaks without it

Without cwnd, we would send as fast as rwnd allows, 65535 bytes at once, whatever the path between us
can carry. A router with a smaller queue would drop the tail of every window. Every drop costs a
retransmission, which adds more load to the same full queue. With many senders, the network gets busy
carrying copies (congestion collapse). Reno backs off on each loss and gives every sender a fair share
(slide 3-121: AIMD has "desirable stability properties").

### 4.7 Demo

```sh
python streaprous/docker/lab.py demo trace
```

The server runs with `STREAPROUS_DROP_SEND=150,400-430` (one loss, then a burst of 31), and netem adds
20 ms delay on each side so the RTT is about 40 ms. A Linux client downloads `demo_1m.bin`. The demo
prints the `STATE`, `FAST_RETRANSMIT` and `TIMEOUT` lines and draws `out/trace/cwnd.svg`: cwnd
(solid), ssthresh (dashed) and rwnd (thin) in KB against time, with red marks at timeouts and orange
marks at fast retransmits. Open the SVG in a browser and walk along it:

```text
[Streap_tcp] 172.28.0.20:40434 STATE old=SLOW_START new=CONGESTION_AVOIDANCE | cwnd=33580 ssthresh=32768 rwnd=65535 flight=30660 adv=32768
[Streap_tcp] 172.28.0.20:40434 STATE old=CONGESTION_AVOIDANCE new=FAST_RECOVERY | cwnd=24780 ssthresh=20400 rwnd=65535 flight=39420 adv=32768
[Streap_tcp] 172.28.0.20:40434 FAST_RETRANSMIT seq=219541 len=1460 | cwnd=24780 ssthresh=20400 rwnd=65535 flight=39420 adv=32768
[Streap_tcp] 172.28.0.20:40434 STATE old=FAST_RECOVERY new=CONGESTION_AVOIDANCE | cwnd=20400 ssthresh=20400 rwnd=65535 flight=17520 adv=32768
[Streap_tcp] 172.28.0.20:40434 STATE old=CONGESTION_AVOIDANCE new=SLOW_START | cwnd=1460 ssthresh=18451 rwnd=65535 flight=36500 adv=32768
[Streap_tcp] 172.28.0.20:40434 TIMEOUT seq=584541 len=1460 rto=0.400 | cwnd=1460 ssthresh=18451 rwnd=65535 flight=1460 adv=32768
[Streap_tcp] 172.28.0.20:40434 STATE old=SLOW_START new=CONGESTION_AVOIDANCE | cwnd=18980 ssthresh=18451 rwnd=65535 flight=16060 adv=32768
```

On the chart: a steep climb (slow start) to 33580, a gentle slope (congestion avoidance), a drop to
24780 with a short rise (fast recovery), back to 20400, a fall to 1460 (the burst ends in a timeout),
and slow start again up to 18451. The end of the client's run:

```text
RESULT method=GET path=/demo_1m.bin status=200 body_bytes=1048576 sha256=3dbbefab3a76b40176de21b69bb7555e09c72c4714199395c82d9a746446dcb0 expect=ok handshake=ok seconds=3.457
```

The `loss` demo (Part 2) also shows Reno at work: the ssthresh floor at the first timeout, and fast
recovery from cwnd 15726 at the second loss.

### 4.8 Self-check questions

1. cwnd is 33580 in congestion avoidance with MSS 1460. What is cwnd after the next new ACK?
   *Answer:* 33580 + 2131600 // 33580 = 33580 + 63 = 33643.
2. cwnd is 40800 when the third duplicate ACK arrives. What are ssthresh and cwnd, and in which state?
   *Answer:* ssthresh 20400, cwnd 20400 + 3 × 1460 = 24780, FAST_RECOVERY, and the segment at
   `snd_una` is retransmitted at once.
3. Why does a timeout cut cwnd to 1 MSS when a triple duplicate ACK only halves it?
   *Answer:* duplicate ACKs prove later segments still get through, so the network is only somewhat
   full. A timeout means nothing came back at all, a much worse sign, so we start over with slow start
   (slides 3-121, 3-125).

---

## Part 5. Our hooks in the teacher's files

Every changed hunk in a teacher file carries a `# TEAM: <reason>` comment line, and
`tools/check_hunks.py` checks that it does. The full list:

| File | Function | What our change does |
| --- | --- | --- |
| `streaprous_socket/streaprous_raw_connection.py` | module top (imports) | imports `RECV_BUFFER` from `tcp_config.py`, the default window of the header builders |
| `streaprous_socket/streaprous_raw_connection.py` | `build_tcp_packet` | new `window=RECV_BUFFER` argument replaces the fixed `window = 5840` (slide 3-94) |
| `streaprous_socket/streaprous_raw_connection.py` | `send_tcp_data_segment` | same `window` argument, so each data segment advertises our free buffer space |
| `streaprous_socket/streaprous_raw_connection.py` | `accept` | after the duplicate-handshake check, hands the SYN's TCP header to `note_client_syn`, which keeps its MSS option, window and sequence number; nothing else in `accept` changes |
| `streaprous_socket/streaprous_raw_connection.py` | `run_conn_recv` | new `max_bytes` and `timeout` keywords; the body returns `get_connection(s, conn_info).recv(max_bytes, timeout)`, the next in-order bytes of the stream |
| `streaprous_socket/streaprous_raw_connection.py` | `run_conn_send` | returns `get_connection(...).send(payload)`, which cuts segments, retransmits and waits for every ACK |
| `streaprous_socket/streaprous_raw_connection.py` | `close_connection` | calls `finish_connection` (FIN until ACKed, ACK of the client's FIN) before the teacher's removal from `connections` |
| `daemon/request.py` | `Request.prepare` | fills the Lab 3 gap: `fetch_headers_body`, `self.body`, `prepare_headers` |
| `daemon/httpadapter.py` | `HttpAdapter.handle_client_raw` | reads with `read_request_raw`, answers `400 Bad Request` on a parse error, sends with `run_conn_send`, removes the 5 s sleep, and always calls `close_connection` once (`try/finally`) |
| `daemon/httpadapter.py` | `HttpAdapter.read_request_raw` | new method: reassembles one HTTP request from the byte stream (headers to `\r\n\r\n`, then `Content-Length` body bytes) |

The SYN-ACK in `accept` still calls `build_tcp_packet` without `window`. So it advertises the whole
buffer, which is exactly our free space at that moment. The teacher's handshake, dispatcher,
`start_backend.py` and `daemon/backend.py` are unchanged. The TCP modules are imported inside these
functions, as the teacher does with `from start_backend import detect_interface`, so there is no import
cycle.

---

## Part 6. Background: what does not come from the slides

Everything below is our choice or comes from a standard outside the course slides. The teacher may
ask where a number comes from; this is the answer.

| Item | Where in our code | Source and reason |
| --- | --- | --- |
| **Background:** delayed-ACK time 0.1 s | `tcp_config.py` `delayed_ack`, `TcpReceiver.on_segment` row 1 | Slide 3-85 says "wait up to 500ms". We wait 0.1 s, which is under the slide's limit and under the client's 200 ms minimum timeout, so a delayed ACK never makes the client retransmit. |
| **Background:** first-sample RTT rule | `RttEstimator.add_sample` | RFC 6298: the first sample sets EstimatedRTT = sample and DevRTT = sample / 2, because there is nothing to average with yet. |
| **Background:** RTO doubling | `RttEstimator.back_off` | RFC 6298 (slide 3-84 only says "restart timer"): each timeout doubles the timeout; the next valid sample recomputes it. |
| **Background:** 0.2 s floor and 4 s ceiling | `tcp_config.py` `min_rto`, `max_rto` | Our values. The floor stops needless resends when Docker's RTT is about 1 ms (our Lab 2 app had exactly that problem). The ceiling keeps a stuck demo short. RFC 6298 suggests a 1 s floor; the Linux client uses 0.2 s. |
| **Background:** ssthresh floor of 2 MSS | `RenoCongestion._halve_ssthresh` | RFC 5681. Without it, a timeout at a tiny cwnd would give a tiny ssthresh, and slow start would end almost at once. |
| **Background:** definition of a duplicate ACK | `TcpSender.on_ack`, `TcpConnection.process_packet` | RFC 5681: the same ACK number, data outstanding, no data and no FIN in the segment, and an unchanged window. Window updates and data segments are not loss signals. |
| **Background:** go-back after a timeout | `TcpSender._on_timeout` (`snd_nxt = snd_una`) | BSD Reno. Resends the rest of the window as cwnd allows, so a burst of losses costs one timeout. Slide 3-84 only resends the one segment. |
| **Background:** pure ACKs carry `snd_max` | `TcpConnection._send_ack` | BSD `tcp_output`. After a go-back, an ACK never carries a sequence number below what the client already has. |
| **Background:** persist timer and one-byte probe | `TcpSender._check_persist`, `TcpSender._send_window_probe` | RFC 1122 and RFC 9293. Slide 3-94 says the sender limits in-flight data to rwnd but not what to do at rwnd = 0. The probe carries one real byte at `snd_nxt`, and the receiver must answer with its window. |
| **Background:** probe interval rule F3 | `persist_min` 0.2 s, `persist_max` 2 s | Our Lab 2 design: first probe after 0.2 s, interval doubles up to 2 s, back to 0.2 s when the window opens. |
| **Background:** window-update rule F4 | `TcpReceiver.window_update_due`, `TcpConnection.recv` | Our Lab 2 design: send an update at once when the last advertised window was below RcvBuffer/4 and the free space is now at least RcvBuffer/4. It saves an extra ACK after every small read, keeps a stalled sender from being told about tiny windows (silly window syndrome, RFC 1122), and still tells it quickly when real room opens. |
| **Background:** 536-byte default MSS | `tcp_config.py` `default_peer_mss`, `get_connection` | RFC 879: a peer that sends no MSS option is assumed to accept 536-byte segments. Our SYN-ACK has no options, so clients send us 536-byte segments. We send `min(1460, client's MSS option)`. |
| **Background:** checksums not verified | `parse_tcp_packet` | In Docker, all 31 client packets we tested had an unfinished checksum field when read on the raw socket. The virtual network card leaves the checksum to hardware that does not exist ("checksum offload"). Verifying would drop every packet. The segments we send still carry the teacher's correct checksum. |
| **Background:** short segments wait while data is in flight | `TcpSender._send_allowed` | Like Nagle's rule (RFC 896): a piece shorter than one MSS waits for ACKs unless it is the last piece of data or nothing is in flight. |
| **Background:** closest-integer unwrap of sequence numbers | `unwrap` | Standard modular arithmetic on the 32-bit sequence space. |
| **Background:** limits for giving up | `max_retries` 8, `peer_silence` 60 s, `linger` 2 s, `request_timeout` 30 s | Our values, so a dead peer cannot hold a connection forever. |
| **Background:** first client byte from the SYN | `note_client_syn`, `TcpConnection.__init__` | Slide 3-96 says the handshake agrees on the starting sequence numbers. We take the client's from its SYN, because a reordered data segment can arrive before the handshake ACK. |

---

## Part 7. The whole story in one picture

```text
  application bytes (HTTP response)
          |
          v
  TcpSender.write --> _send_allowed: in flight <= min(cwnd, rwnd)
          |                 ^                ^
          |                 |                |
          |      RenoCongestion.cwnd   client's window in each ACK (_update_rwnd)
          |      grows on new ACKs,     0 -> persist timer -> one-byte probe
          |      halves on 3 dup ACKs,
          |      1 MSS on timeout
          v
  segments (MSS 1460) --> network (may lose, duplicate, reorder) --> client TCP
          ^                                                             |
          |        ACK = next byte expected (cumulative), window        |
          +-------------------------------------------------------------+
     new ACK:     snd_una moves, RTT sample, timer restarts, cwnd grows
     3 dup ACKs:  fast retransmit, fast recovery
     no ACK:      timeout (RTO = EstimatedRTT + 4*DevRTT, doubled), go back, slow start

  receiving (HTTP request): TcpReceiver.on_segment
     in order -> ready buffer -> read() by the application -> window grows -> window update
     gap      -> out_of_order store + duplicate ACK; filled -> ACK at once
     window() = RcvBuffer - unread bytes, sent in every segment we send
```

What to remember for each feature:

- **Reliable transfer:** cumulative ACKs, one timer for the oldest unACKed byte, timeout and fast
  retransmit, RTT-based timeout, receiver ordering with the four slide 3-85 rules.
- **Flow control:** window = RcvBuffer − unread bytes; the sender keeps in flight ≤ rwnd; a zero
  window is broken by our update (F4) or the sender's probe (F3).
- **Congestion control:** cwnd from slow start (+1 MSS per ACK), congestion avoidance (+MSS·MSS/cwnd),
  fast recovery (ssthresh + 3 MSS), timeout (1 MSS); ssthresh = half of cwnd at a loss.
- **Where they meet:** `TcpSender._send_allowed`, `min(cwnd, rwnd) − in flight`.

---

## Appendix A. Every function, module by module

Every function we wrote, with one line each. The pair that owns it is in brackets; "shared" means
plumbing everyone should recognise.

### `tcp_sender.py` (class `TcpSender`)

- `write` (reliable): takes the next block of application data and sends what the window allows.
- `close` (reliable): asks for a FIN after the last data byte.
- `on_ack` (reliable, flow): handles one ACK number and window; new, duplicate, old or impossible.
- `on_tick` (reliable, flow): runs the retransmission timer and the persist timer.
- `bytes_in_flight` (all): `snd_nxt − snd_una`.
- `data_end` (reliable): the sequence number just after the last data byte.
- `data_acked` (reliable): True when every data byte is ACKed.
- `fin_acked` (reliable): True when our FIN is ACKed.
- `next_deadline` (shared): the earlier of the two timer deadlines.
- `_usable_window` (flow, congestion): `min(cwnd, rwnd) − bytes_in_flight()`.
- `_send_allowed` (all): the send loop of 1.5.
- `_payload` (reliable): cuts `length` bytes at `seq` from the buffer.
- `_segment_at_una` (reliable): payload or FIN of the oldest unACKed segment.
- `_emit` (shared): makes one `SendSegment` and counts it.
- `_start_timer` / `_stop_timer` (reliable): the one retransmission timer (slide 3-84).
- `_cancel_rtt_timing` (reliable): forgets the segment being timed.
- `_take_rtt_sample` (reliable): SampleRTT when the timed segment is ACKed (slide 3-81).
- `_on_timeout` (reliable, congestion): timeout handling (slides 3-84, 3-86).
- `_on_new_ack` (reliable, congestion): a new cumulative ACK (slides 3-84, 3-87).
- `_on_duplicate_ack` (reliable, congestion): counts it and may fast retransmit (slide 3-88).
- `_fast_retransmit` (reliable, congestion): resends the segment at `snd_una` (slide 3-88).
- `_update_rwnd` (flow): takes the client's window; `RWND_ZERO` / `RWND_OPEN`.
- `_probe_needed` (flow): rwnd 0, nothing in flight, data waiting.
- `_check_persist` (flow): arms or disarms the persist timer.
- `_send_window_probe` (flow): one byte at `snd_nxt` to ask for the window.

### `tcp_receiver.py` (class `TcpReceiver`)

- `on_segment` (reliable, flow): picks the slide 3-85 rule and trims to the window.
- `_deliver_in_order` (reliable): appends in-order bytes and moves `rcv_nxt` (slide 3-80).
- `_store_out_of_order` (reliable): keeps a segment above the gap.
- `_pull_from_out_of_order` (reliable): moves stored segments in once the gap is filled.
- `on_tick` (reliable): the delayed-ACK timer.
- `read` (flow): hands bytes to the application, which opens the window.
- `readable` (shared): bytes ready or FIN received.
- `window` (flow): `RcvBuffer − (LastByteRcvd − LastByteRead)` (slide 3-94).
- `window_update_due` (flow): rule F4.
- `ack_sent` (reliable, flow): a segment with `ack = rcv_nxt` went out; remember its window.
- `next_deadline` (shared): the delayed-ACK deadline.

### `tcp_rtt.py` (class `RttEstimator`)

- `add_sample` (reliable): EstimatedRTT, DevRTT and the timeout (slides 3-82, 3-83).
- `back_off` (reliable): doubles the timeout, capped at 4 s (slide 3-84).

### `tcp_reno.py` (class `RenoCongestion`)

- `on_new_ack` (congestion): slow start, congestion avoidance, end of fast recovery (slides 3-123, 3-125).
- `on_duplicate_ack` (congestion): counts; the third starts fast recovery (slide 3-125).
- `on_timeout` (congestion): ssthresh = half, cwnd = 1 MSS, slow start (slides 3-124, 3-125).
- `_set_state` (congestion): changes state and logs `STATE`.
- `_halve_ssthresh` (congestion): `max(cwnd // 2, 2 * MSS)`.

### `tcp_connection.py` (class `TcpConnection` and the registry)

- `start` (shared): logs `OPEN` and starts the worker thread.
- `_worker` (shared): takes up to 64 packets from the queue, handles them, runs the timers.
- `process_packet` (shared): ACK field to the sender, data and FIN to the receiver.
- `process_timers` (shared): sender timers, delayed ACK, the 60 s silence limit.
- `_transmit` (shared): puts the sender's segments on the wire with `ack = rcv_nxt` and our window.
- `_sendto` (shared): adds the IP header and sends.
- `_send_ack` (reliable, flow): a pure ACK (slides 3-85, 3-95).
- `_note_advertised` (flow): remembers the window we sent; logs `WINDOW_ZERO`.
- `_state` (shared): the numbers of every log line and CSV row.
- `_on_event` (shared): passes one event to the trace.
- `_update_table` (shared): writes our sequence numbers into the teacher's `connections` entry.
- `_broken` (shared): True after an RST or a give-up.
- `recv` (reliable, flow): next in-order bytes; sends a window update when due.
- `send` (reliable): sends a payload and returns when every byte is ACKed.
- `close` (reliable): FIN until ACKed, then waits for the client's FIN (slide 3-96).
- `note_client_syn` (shared): called by `accept`; keeps the SYN's MSS option, window and sequence number.
- `get_connection` (shared): finds or creates the `TcpConnection` of a `conn_info`.
- `finish_connection` (shared): closes it and removes it from the registry.

### `tcp_segment.py`

- `parse_tcp_packet` (shared): IP datagram bytes → `Segment` (ports, seq, ack, flags, window, payload).
- `parse_mss_option` (shared): the MSS option of the client's SYN.
- `wrap` (reliable): sequence number → 32 bits.
- `unwrap` (reliable): 32 bits → the closest unbounded number (slide 3-79).
- `flag_names` (shared): `0x12` → `"SYN,ACK"`.

### `tcp_config.py`

- `parse_drop_list` (demos): `"3,60,400-430"` → a set of numbers.
- `load_settings` (shared): every setting from environment variables.
- `describe` (shared): the settings on one line, for the `OPEN` line.
- `_read_int`, `_read_float`, `_read_drops` (shared): read and range-check one variable.
- `_drops_text` (shared): a drop set back to text.

### `tcp_loss.py` (class `LossInjector`)

- `check_send` (demos): drop this outgoing data segment? (the n-th first transmission, or random).
- `check_recv` (demos): drop this incoming data segment? (the n-th one).

### `tcp_trace.py` (class `TcpTrace` and helpers)

- `event` (shared): prints the log line at its level and writes a CSV row.
- `_write_row` (shared): appends one CSV row, creating the file first.
- `close` (shared): closes the CSV file.
- `format_line` (shared): builds one `[Streap_tcp]` line.
- `_value_text` (shared): floats with 3 decimals.

### `daemon/httpadapter.py`

- `HttpAdapter.read_request_raw` (reliable): reads one whole HTTP request from the byte stream.

---

## Appendix B. Tools, demo client and Docker driver

All commands run from the repo root unless marked.

**Tools** (`streaprous/tools/`, standard library only):

- `python streaprous/tools/plot_trace.py TRACE.csv [-o OUT.svg] [--window]`: draws a trace as an SVG
  chart: cwnd, ssthresh and rwnd, or with `--window` our advertised window.
- `python streaprous/tools/check_hunks.py [--base ccc6e34] [--worktree]`: checks that every changed
  hunk of a teacher file has a `# TEAM` line, and that `daemon/` changes stay inside the three allowed
  functions.
- `python streaprous/tools/check_imports.py`: checks that the backend loads only the standard library
  and `netifaces`.
- `python streaprous/tools/check_walkthrough.py`: checks that this document names every function and
  hook and cites only slides from our list.

**Demo client and files** (`streaprous/demo/`, runs in the client container and on Windows):

- `python demo/make_files.py` (from `streaprous/`): writes `apps/demo_256k.bin` and `apps/demo_1m.bin`
  with deterministic content and prints their sha256.
- `python streaprous/demo/client.py get URL [--expect NAME] [--rcvbuf N] [--read-bytes N]
  [--read-delay S] [--pause-after N] [--pause S] [--header-bytes N]`: one GET; can read slowly and
  pause; checks the body against the expected file.
- `python streaprous/demo/client.py post URL --size N`: one POST with a deterministic body of N bytes.
- `python streaprous/demo/client.py connect-close HOST PORT [--no-fin-check]`: handshake, close, and
  wait for the server's FIN.
- Each client run ends with one `RESULT k=v ...` line and exits 0 on success.

**Docker driver** (`streaprous/docker/lab.py`, runs on Windows, needs Docker Desktop):

- `python streaprous/docker/lab.py build`: builds the two images (Python 3.10 and Python 3.6).
- `python streaprous/docker/lab.py smoke`: starts the server and checks one handshake.
- `python streaprous/docker/lab.py test [--only a,b]`: runs and checks every scenario (`lab3`,
  `download`, `http`, `loss`, `random-loss`, `slow-reader`, `trace`, `upload`, `slow-app`, `py36`).
- `python streaprous/docker/lab.py demo loss|slow-reader|trace|slow-app`: runs one scenario and prints
  its proof lines.
- `python streaprous/docker/lab.py unit36`: compiles everything and runs the unit tests under Python
  3.6.
- `python streaprous/docker/lab.py clean`: removes the containers and the network.
- Each run leaves `server.log`, `client.txt`, `server.pcap`, the CSV trace and any chart in
  `streaprous/out/<scenario>/`.
