# SDP → SAP Proxy

A lightweight web UI and SAP announcer for AES67/RTP environments where the source exposes SDP/NMOS but a receiver only discovers streams using SAP.

Typical use:

```text
Riedel Artist / AES67-UIC
      | RTP multicast
      +-------------------------------> SAP-only receiver
      |
      | exported SDP
      v
SDP → SAP Proxy ---- SAP multicast ---> SAP-only receiver
```

The proxy does **not** relay audio. It only sends periodic SAP announcements containing the SDP.

## Features

- Upload an `.sdp` file or paste SDP in the browser.
- Basic SDP validation.
- Periodic SAP multicast announcements.
- Pause/resume individual sessions.
- Trigger an announcement immediately.
- Multiple simultaneous SDP sessions.
- Docker-friendly and intended to run directly on the AES67 media VLAN.

## Quick start with Docker

```bash
docker compose up -d --build
```

Open:

```text
http://SERVER-IP:8000
```

`network_mode: host` is intentional. Multicast generally behaves much more predictably this way than through Docker's bridge/NAT network.

## Important AES67 network setting

On a host with more than one NIC/VLAN, set `SAP_INTERFACE` in `docker-compose.yml` to the **local IPv4 address of the interface on the AES67 media VLAN**.

Example:

```yaml
environment:
  SAP_INTERFACE: 172.16.102.50
```

The default SAP group and port are:

- `224.2.127.254`
- UDP `9875`

## Run without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

If required:

```bash
export SAP_INTERFACE=172.16.102.50
export SAP_INTERVAL=30
```

## Example SDP

```sdp
v=0
o=- 123456 1 IN IP4 172.16.102.20
s=Artist Program 1
c=IN IP4 239.100.10.1/32
t=0 0
m=audio 5004 RTP/AVP 96
a=rtpmap:96 L24/48000/2
a=recvonly
```

For production interoperability, keep the SDP exported by Artist intact rather than recreating it manually. Attributes such as `ts-refclk`, `mediaclk`, `ptime`, channel ordering, source-filter, and exact RTP parameters may be required by the receiving device.

## Current limitations

- Configured sessions are in memory only and are lost on restart.
- No authentication on the web UI.
- No automatic NMOS registry/client integration yet.
- SAP deletion packets are not emitted when a session is deleted/paused.

These are deliberately kept out of v0.1 so the initial proxy is small and easy to test on an AES67 VLAN.
