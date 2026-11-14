# Lab image: Python 3.10 backend and real Linux TCP clients (course lab, Ubuntu 22.04).
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-netifaces iptables iproute2 tcpdump curl procps \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /w
