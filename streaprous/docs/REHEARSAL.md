# Rehearsal: every demo on two virtual machines

The server runs on a virtual machine and the client runs on Windows. Do this once on each VM:
Ubuntu (VMware) and TinyCore (VirtualBox). The lines marked "Prints" are real output captured in
Docker. Your IP addresses, ports, times and sequence numbers will differ; the event names and the
shape of the lines will not. (In the examples the client shows as 172.28.0.20, the Docker client;
on your run it is the Windows address.)

Terminals you need:

- W1: a Windows terminal (Git Bash) in the repo root, `c:\Users\Admin\Desktop\Mạng Máy Tính\Assignment\streamlink`.
- W2: a second Windows terminal for the web server that hands files to the VM.
- V1: a terminal on the VM for the backend.
- V2: a second terminal on the VM for the chart (demos c and d).

Addresses used below:

| | Ubuntu (VMware) | TinyCore (VirtualBox) |
| --- | --- | --- |
| VM address | 192.168.109.128 | 192.168.56.101 |
| Windows address on that network | 192.168.109.1 (adapter VMnet8) | 192.168.56.1 (host-only) |
| Interface for netem | ens33 | none, TinyCore has no `tc` |

If the Windows address differs, run `ipconfig` and look for the adapter named VMnet8 (Ubuntu) or the
VirtualBox Host-Only adapter (TinyCore).

## Part 1. Ubuntu VM (192.168.109.128)

### 1.1 Copy the code

In W1:

```sh
git archive --format=tar.gz -o streaprous.tar.gz HEAD streaprous
python -m http.server 8000 --bind 192.168.109.1
```

The second command stays running (use W2 for it). Windows may show a firewall prompt for Python:
allow it on private networks.

Prints: `Serving HTTP on 192.168.109.1 port 8000 (http://192.168.109.1:8000/) ...`

On the VM (V1):

```sh
wget http://192.168.109.1:8000/streaprous.tar.gz
tar xzf streaprous.tar.gz
cd streaprous
```

Prints: a progress bar ending with `saved`, and one `GET /streaprous.tar.gz` line in W2.

If `wget` cannot connect: run `ipconfig` on Windows, check the VMnet8 address, use it in the `--bind`
option and in the `wget` URL, and allow Python through the Windows firewall (Windows Security,
Firewall, Allow an app). Test with `ping 192.168.109.1` from the VM.

### 1.2 Check netifaces and make the demo files

```sh
python3 -c "import netifaces; print(netifaces.version)"
python3 demo/make_files.py
```

The package python3-netifaces is already installed, so the first line prints a version. The second
prints one line per file, `<name> <size> <sha256>`:

```
demo_256k.bin 262144 ad8642f375339abce96a5e7a8ecb0739fe0b499fa5592ea3b447d238e7b53ba8
demo_1m.bin 1048576 3dbbefab3a76b40176de21b69bb7555e09c72c4714199395c82d9a746446dcb0
```

The files are written to `apps/`. The hashes are the same on every machine because the content is
deterministic; the client compares against them.

### 1.3 Optional: slower network for a wider chart

```sh
sudo tc qdisc add dev ens33 root netem delay 20ms
```

This adds 20 ms to everything the VM sends, so the chart in demo c has more room between the steps.
It is not verified on this VM. If `ens33` is not the interface name, find it with `ip addr`. When
you are done with all demos, remove it:

```sh
sudo tc qdisc del dev ens33 root
```

### 1.4 Start the backend

The backend needs root and the VM's own address (not 127.0.0.1). You start it again with different
environment settings for each demo, and stop it with Ctrl+C in V1 in between.

```sh
sudo python3 start_backend.py 192.168.109.128 9000
```

Prints: `Waiting for incoming connections` (among the teacher's `[Streap_raw]` lines). Leave it running.
Environment switches go in front of the command through `sudo env NAME=value ...`, as shown below.

### 1.5 Demos

Before each demo delete old traces so the chart uses the right file: `sudo rm -f out/trace_*.csv`.
In W1 set the address once: `URL=http://192.168.109.128:9000`.

#### Demo a (loss): loss, timeout and fast retransmit

V1:

```sh
sudo env STREAPROUS_DROP_SEND=3,60 python3 start_backend.py 192.168.109.128 9000
```

W1:

```sh
python streaprous/demo/client.py get $URL/demo_1m.bin --expect demo_1m.bin
```

Prints on the client, last line:

```
RESULT method=GET path=/demo_1m.bin status=200 body_bytes=1048576 sha256=3dbbefab... expect=ok handshake=ok seconds=1.412
```

`expect=ok` means every byte arrived and matches. Prints in V1, look for these lines:

```
[Streap_tcp] 172.28.0.20:35486 DROP dir=send why=nth=3 seq=4921 len=1460 | cwnd=2920 ssthresh=32768 rwnd=65535 flight=2920 adv=32768
[Streap_tcp] 172.28.0.20:35486 DROP dir=send why=nth=60 seq=88141 len=1460 | cwnd=14600 ssthresh=2920 rwnd=65535 flight=14600 adv=32768
[Streap_tcp] 172.28.0.20:35486 DUPACK ack=4921 count=1 | cwnd=4380 ssthresh=32768 rwnd=65535 flight=4380 adv=32768
[Streap_tcp] 172.28.0.20:35486 TIMEOUT seq=4921 len=1460 rto=0.400 | cwnd=1460 ssthresh=2920 rwnd=65535 flight=1460 adv=32768
[Streap_tcp] 172.28.0.20:35486 FAST_RETRANSMIT seq=88141 len=1460 | cwnd=12243 ssthresh=7863 rwnd=65535 flight=14600 adv=32768
```

Two `DROP` lines (segments 3 and 60). Segment 3 is lost early, when too few segments follow it for
three duplicate ACKs, so it ends in a `TIMEOUT` and cwnd falls to 1 MSS. Segment 60 is lost with
many segments behind it, so three `DUPACK` lines lead to `FAST_RETRANSMIT` and cwnd only halves.

#### Demo b (slow-reader): zero window and probe

V1:

```sh
sudo python3 start_backend.py 192.168.109.128 9000
```

W1:

```sh
python streaprous/demo/client.py get $URL/demo_256k.bin --expect demo_256k.bin --rcvbuf 4096 --read-bytes 4096 --read-delay 0.05 --pause-after 32768 --pause 2.0
```

The client reads slowly and sleeps for 2 s after 32768 bytes, so its buffer fills.

Prints on the client:

```
RESULT method=GET path=/demo_256k.bin status=200 body_bytes=262144 sha256=ad8642f3... expect=ok handshake=ok seconds=6.632
```

Prints in V1:

```
[Streap_tcp] 172.28.0.20:54352 RWND_ZERO ack=39961 | cwnd=21900 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:54352 PROBE seq=39961 interval=0.200 | cwnd=23360 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:54352 PROBE seq=39961 interval=0.400 | cwnd=23360 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:54352 PROBE seq=39961 interval=0.800 | cwnd=23360 ssthresh=32768 rwnd=0 flight=0 adv=32768
[Streap_tcp] 172.28.0.20:54352 RWND_OPEN rwnd=2920 | cwnd=13140 ssthresh=32768 rwnd=2920 flight=0 adv=32768
```

`RWND_ZERO`: the client advertised window 0 and the server stops sending. `PROBE`: one byte every
0.5 s (the saved lines above come from our first version, when the interval doubled from 0.2 s).
`RWND_OPEN`: the client read, the window is open again, sending resumes.
If there is no `RWND_ZERO`, see Troubleshooting.

#### Demo c (trace): congestion window chart

V1 (optionally with the netem from 1.3 on):

```sh
sudo env STREAPROUS_DROP_SEND=150,400-430 python3 start_backend.py 192.168.109.128 9000
```

W1:

```sh
python streaprous/demo/client.py get $URL/demo_1m.bin --expect demo_1m.bin
```

Prints on the client: a `RESULT` line with `status=200 ... expect=ok`. Prints in V1:

```
[Streap_tcp] 172.28.0.20:48726 STATE old=SLOW_START new=CONGESTION_AVOIDANCE | cwnd=33580 ssthresh=32768 rwnd=65535 flight=30660 adv=32768
[Streap_tcp] 172.28.0.20:48726 STATE old=CONGESTION_AVOIDANCE new=FAST_RECOVERY | cwnd=24780 ssthresh=20400 rwnd=65535 flight=39420 adv=32768
[Streap_tcp] 172.28.0.20:48726 FAST_RETRANSMIT seq=621041 len=1460 | cwnd=14573 ssthresh=10193 rwnd=65535 flight=18980 adv=32768
[Streap_tcp] 172.28.0.20:48726 TIMEOUT seq=584541 len=1460 rto=0.400 | cwnd=1460 ssthresh=18451 rwnd=65535 flight=1460 adv=32768
```

Stop the backend (Ctrl+C in V1), then make the chart in V2 (or V1), from the `streaprous` folder:

```sh
ls out
sudo python3 tools/plot_trace.py out/trace_<ip>_<port>.csv -o out/cwnd.svg
cd out && python3 -m http.server 8001
```

Use the real file name shown by `ls out` (the client's address and port). The `sudo` is there
because the backend, running as root, owns `out/`. The plot tool prints the output path and a count
of each event. Open `http://192.168.109.128:8001/cwnd.svg` in a browser on Windows. You should see
cwnd (solid), ssthresh (dashed) and rwnd (thin) against time: the climb of slow start, the bend at
ssthresh, the sawtooth, red marks at timeouts and orange marks at fast retransmits. Stop the web
server with Ctrl+C.

#### Demo d (slow-app): slow application, receive window chart

V1:

```sh
sudo env STREAPROUS_RCVBUF=16384 STREAPROUS_APP_READ_DELAY=0.05 STREAPROUS_APP_READ_BYTES=2048 python3 start_backend.py 192.168.109.128 9000
```

The server's own application now reads slowly: its receive buffer is 16384 bytes and the
application takes 2048 bytes every 0.05 s. The client uploads, so the server is the receiver.

W1:

```sh
python streaprous/demo/client.py post $URL/ --size 300000
```

Prints on the client (one long line; `sent_sha256` is the hash of what the client sent):

```
RESULT method=POST path=/ status=200 body_bytes=251 sha256=423a7d55... sent_bytes=300000 sent_sha256=5dd581b0...
```

Prints in V1:

```
[Streap_tcp] 172.28.0.20:39868 WINDOW_ZERO ack=3098382862 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=0
[Streap_tcp] 172.28.0.20:39868 WINDOW_UPDATE window=4096 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=4096
[Streap_tcp] 172.28.0.20:39868 WINDOW_UPDATE window=4320 | cwnd=1460 ssthresh=32768 rwnd=64240 flight=0 adv=4320
```

`WINDOW_ZERO` is the server telling the client its buffer is full (`adv=0`). `WINDOW_UPDATE` is the
server telling the client that room has opened up. Chart (stop the backend first):

```sh
ls out
sudo python3 tools/plot_trace.py out/trace_<ip>_<port>.csv --window -o out/window.svg
cd out && python3 -m http.server 8001
```

Open `http://192.168.109.128:8001/window.svg`. It shows the window the server advertises dropping to
0 and reopening, with marks at each `WINDOW_ZERO` and `WINDOW_UPDATE`.

### 1.6 Clean up

Stop the backend (Ctrl+C) and the web servers, including the `http.server` in W2. If you turned
netem on: `sudo tc qdisc del dev ens33 root`. The backend leaves one iptables rule that drops the
kernel's RST packets for port 9000; it goes away on reboot, or remove it with
`sudo iptables -D OUTPUT -p tcp --sport 9000 --tcp-flags RST RST -j DROP`.

## Part 2. TinyCore VM "NetApp-Serverv2" (192.168.56.101)

TinyCore keeps everything in RAM: after every boot repeat 2.1 and 2.2. The VM has no DNS and no `tc`
(no netem), so loss comes only from `STREAPROUS_DROP_SEND` and `STREAPROUS_DROP_RECV`, and there is
no added delay on the chart. Open a terminal by clicking the terminal icon on the dock.

### 2.1 Copy the code and the netifaces wheel

In W1, from the repo root:

```sh
git archive --format=tar.gz -o streaprous.tar.gz HEAD streaprous
python -m pip download netifaces==0.11.0 --only-binary=:all: --python-version 36 --implementation cp --abi cp36m --platform manylinux1_i686 --no-deps -d .
python -m http.server 8000 --bind 192.168.56.1
```

The wheel file is named `netifaces-0.11.0-cp36-cp36m-manylinux_2_5_i686.manylinux1_i686.whl`. Allow
the Windows firewall prompt for Python. The `--bind` address keeps the folder from being visible on
your Wi-Fi.

On the VM (V1):

```sh
wget http://192.168.56.1:8000/streaprous.tar.gz
wget http://192.168.56.1:8000/netifaces-0.11.0-cp36-cp36m-manylinux_2_5_i686.manylinux1_i686.whl
tar xzf streaprous.tar.gz
```

Prints: a progress bar for each `wget`, and `GET` lines in W2.

### 2.2 Install netifaces from the wheel

```sh
sudo pip3 install --no-index netifaces-0.11.0-cp36-cp36m-manylinux_2_5_i686.manylinux1_i686.whl
```

Prints: `Successfully installed netifaces-0.11.0`. (`sudo pip3 install netifaces` would fail with
"Temporary failure in name resolution" because the VM has no DNS.)

### 2.3 Make the demo files

```sh
cd streaprous
python3 demo/make_files.py
```

Prints the same two lines as in 1.2, with the same hashes.

### 2.4 Start the backend

```sh
sudo python3 start_backend.py 192.168.56.101 9000
```

Prints `Waiting for incoming connections`. For each demo below, start it with the environment shown
there (Ctrl+C between demos), through `sudo env NAME=value python3 start_backend.py ...`.

### 2.5 Demos

Set `URL=http://192.168.56.101:9000` in W1. Before each demo delete old traces:
`sudo rm -f out/trace_*.csv`. The prints are the same as in Part 1.

- Demo a (loss). V1: `sudo env STREAPROUS_DROP_SEND=3,60 python3 start_backend.py 192.168.56.101 9000`.
  W1: `python streaprous/demo/client.py get $URL/demo_1m.bin --expect demo_1m.bin`.
  Look for: client `RESULT ... status=200 ... expect=ok`; in V1 two `DROP` lines, then `DUPACK`,
  `TIMEOUT` and `FAST_RETRANSMIT`.
- Demo b (slow reader). V1: `sudo python3 start_backend.py 192.168.56.101 9000`.
  W1: `python streaprous/demo/client.py get $URL/demo_256k.bin --expect demo_256k.bin --rcvbuf 4096 --read-bytes 4096 --read-delay 0.05 --pause-after 32768 --pause 2.0`.
  Look for: `RWND_ZERO`, repeated `PROBE` with the interval doubling, then `RWND_OPEN`; client `expect=ok`.
- Demo c (cwnd chart). V1: `sudo env STREAPROUS_DROP_SEND=150,400-430 python3 start_backend.py 192.168.56.101 9000`.
  W1: `python streaprous/demo/client.py get $URL/demo_1m.bin --expect demo_1m.bin`.
  Look for: `STATE old=SLOW_START new=CONGESTION_AVOIDANCE`, `FAST_RETRANSMIT`, `TIMEOUT`.
  Then stop the backend, run `ls out`, then
  `sudo python3 tools/plot_trace.py out/trace_<ip>_<port>.csv -o out/cwnd.svg`, then
  `cd out && python3 -m http.server 8001`, and open `http://192.168.56.101:8001/cwnd.svg` on Windows.
  Without netem the transfer is short and the curve is narrow; that is expected.
- Demo d (slow application). V1: `sudo env STREAPROUS_RCVBUF=16384 STREAPROUS_APP_READ_DELAY=0.05 STREAPROUS_APP_READ_BYTES=2048 python3 start_backend.py 192.168.56.101 9000`.
  W1: `python streaprous/demo/client.py post $URL/ --size 300000`.
  Look for: `WINDOW_ZERO` and `WINDOW_UPDATE` in V1, client `status=200` and `sent_bytes=300000`.
  Chart: `sudo python3 tools/plot_trace.py out/trace_<ip>_<port>.csv --window -o out/window.svg`,
  served as above, then open `http://192.168.56.101:8001/window.svg`.

### 2.6 Optional: loss in the client-to-server direction

`STREAPROUS_DROP_RECV=10,20` makes the server ignore its 10th and 20th received data segments once
each. Use it with an upload (`client.py post $URL/ --size 300000`) and look for `DROP dir=recv`
lines, then the client resending.

### 2.7 Clean up

Ctrl+C the backend and both web servers. The iptables rule disappears on reboot.

## Troubleshooting

- `Message too long` in the server log: the VM's network cannot take 1460-byte segments. Restart the
  backend with a smaller segment size: `sudo env STREAPROUS_MSS=1200 python3 start_backend.py <VM_IP> 9000`.
- No `RWND_ZERO` in demo b with the Windows client: the pause was too short for the window to reach
  0. Lengthen it, for example `--pause 4.0`, or lower `--pause-after` (for example 16384).
- The client reports a connection reset, or you see RST packets: the iptables rule that drops the
  kernel's RST is missing. Check `sudo iptables -L OUTPUT -n` for a DROP rule with the RST flag for
  port 9000. `start_backend.py` adds it only when the IP is not 127.x, so start it again with the
  VM's real address.
- Nothing printed on the VM and the client times out: check that the address you gave the backend is
  the VM's (`ip addr` on Ubuntu, `ifconfig eth0` on TinyCore) and not 127.0.0.1, that the client's
  `$URL` uses the same address, and that `ping <VM_IP>` works from Windows.
- `ModuleNotFoundError` for netifaces on TinyCore: you rebooted; repeat 2.1 and 2.2.
- `wget` cannot reach Windows: check the `--bind` address against `ipconfig`, and the Windows
  firewall prompt for Python.
- `Permission denied` when plotting: the trace files belong to root; use `sudo python3 tools/plot_trace.py`.
- Chart is empty or shows the wrong transfer: `ls out`, pick the right `trace_*.csv`, and delete old
  ones before the demo.
