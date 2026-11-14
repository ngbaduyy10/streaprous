# Lab image for Python 3.6: the same backend on the interpreter the course VMs run.
FROM python:3.6-slim
# The security.debian.org line of this old release no longer answers, so apt drops it first.
RUN sed -i "/security.debian.org/d" /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends iptables iproute2 tcpdump \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir netifaces==0.11.0
WORKDIR /w
