import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import socket
import struct
import time
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

SAP_GROUP = os.getenv('SAP_GROUP', '224.2.127.254')
SAP_PORT = int(os.getenv('SAP_PORT', '9875'))
SAP_INTERVAL = float(os.getenv('SAP_INTERVAL', '30'))
SAP_TTL = int(os.getenv('SAP_TTL', '255'))
SAP_INTERFACE = os.getenv('SAP_INTERFACE', '')
DATA_DIR = os.getenv('DATA_DIR', '/data')
DATA_FILE = os.path.join(DATA_DIR, 'announcements.json')

app = FastAPI(title='SDP to SAP Proxy')
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')

@dataclass
class Announcement:
    id: str
    name: str
    sdp: str
    enabled: bool = True
    created_at: float = 0.0
    last_sent: Optional[float] = None
    last_error: Optional[str] = None

announcements: Dict[str, Announcement] = {}
_task: Optional[asyncio.Task] = None



def _save_announcements() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_file = DATA_FILE + '.tmp'
    payload = [
        {
            'id': ann.id,
            'name': ann.name,
            'sdp': ann.sdp,
            'enabled': ann.enabled,
            'created_at': ann.created_at,
        }
        for ann in announcements.values()
    ]
    with open(tmp_file, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_file, DATA_FILE)


def _load_announcements() -> None:
    announcements.clear()
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        for item in payload:
            ann = Announcement(
                id=str(item['id']),
                name=str(item['name']),
                sdp=str(item['sdp']),
                enabled=bool(item.get('enabled', True)),
                created_at=float(item.get('created_at', time.time())),
            )
            _validate_sdp(ann.sdp)
            announcements[ann.id] = ann
    except Exception as exc:
        print(f'WARNING: Could not load {DATA_FILE}: {exc}', flush=True)


def _origin_ip_from_sdp(sdp: str) -> str:
    for line in sdp.replace('\r\n', '\n').split('\n'):
        line = line.strip()
        if line.startswith('o='):
            parts = line[2:].split()
            if len(parts) >= 6:
                candidate = parts[-1]
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    pass
    return '127.0.0.1'


def _session_name_from_sdp(sdp: str) -> str:
    for line in sdp.replace('\r\n', '\n').split('\n'):
        if line.startswith('s='):
            return line[2:].strip() or 'Unnamed SDP Session'
    return 'Unnamed SDP Session'


def _validate_sdp(sdp: str) -> None:
    normalized = sdp.replace('\r\n', '\n')
    required = ['v=', 'o=', 's=', 'c=', 'm=']
    missing = [prefix for prefix in required if not any(line.startswith(prefix) for line in normalized.split('\n'))]
    if missing:
        raise ValueError(f"Missing required SDP fields: {', '.join(missing)}")


def _sap_packet(sdp: str) -> bytes:
    # SAP v1, IPv4, no auth, no encryption, payload type application/sdp.
    # Header byte: V=1 (bits 7-5 => 001), A=0, R=0, T=0, E=0, C=0 => 0x20.
    first = 0x20
    auth_len = 0

    origin_ip = _origin_ip_from_sdp(sdp)
    try:
        origin_addr = socket.inet_aton(origin_ip)
    except OSError:
        origin_addr = socket.inet_aton('127.0.0.1')

    payload = sdp.encode('utf-8')
    msg_id_hash = struct.unpack('!H', hashlib.sha256(payload).digest()[:2])[0]
    mime = b'application/sdp\x00'
    return struct.pack('!BBH', first, auth_len, msg_id_hash) + origin_addr + mime + payload


def _send_sap(ann: Announcement) -> None:
    packet = _sap_packet(ann.sdp)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, SAP_TTL)
        if SAP_INTERFACE:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(SAP_INTERFACE))
        sock.sendto(packet, (SAP_GROUP, SAP_PORT))
    finally:
        sock.close()


async def _announce_loop():
    while True:
        for ann in list(announcements.values()):
            if not ann.enabled:
                continue
            try:
                await asyncio.to_thread(_send_sap, ann)
                ann.last_sent = time.time()
                ann.last_error = None
            except Exception as exc:
                ann.last_error = str(exc)
        await asyncio.sleep(SAP_INTERVAL)


@app.on_event('startup')
async def startup_event():
    global _task
    _load_announcements()
    _task = asyncio.create_task(_announce_loop())


@app.on_event('shutdown')
async def shutdown_event():
    if _task:
        _task.cancel()


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse('index.html', {
        'request': request,
        'announcements': sorted(announcements.values(), key=lambda a: a.created_at, reverse=True),
        'sap_group': SAP_GROUP,
        'sap_port': SAP_PORT,
        'sap_interval': SAP_INTERVAL,
        'sap_interface': SAP_INTERFACE or 'OS default route',
    })


@app.post('/upload')
async def upload_sdp(name: str = Form(''), sdp_text: str = Form(''), sdp_file: UploadFile | None = File(None)):
    raw = sdp_text.strip()
    if sdp_file and sdp_file.filename:
        raw = (await sdp_file.read()).decode('utf-8', errors='replace').strip()
    if not raw:
        raise HTTPException(400, 'Provide SDP text or upload an .sdp file')
    try:
        _validate_sdp(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    ann_id = hashlib.sha1(f'{time.time_ns()}:{raw}'.encode()).hexdigest()[:12]
    announcements[ann_id] = Announcement(
        id=ann_id,
        name=name.strip() or _session_name_from_sdp(raw),
        sdp=raw,
        enabled=True,
        created_at=time.time(),
    )
    _save_announcements()
    return RedirectResponse('/', status_code=303)


@app.post('/announce/{ann_id}')
async def announce_now(ann_id: str):
    ann = announcements.get(ann_id)
    if not ann:
        raise HTTPException(404, 'Announcement not found')
    try:
        await asyncio.to_thread(_send_sap, ann)
        ann.last_sent = time.time()
        ann.last_error = None
    except Exception as exc:
        ann.last_error = str(exc)
        raise HTTPException(500, str(exc))
    return RedirectResponse('/', status_code=303)


@app.post('/toggle/{ann_id}')
async def toggle(ann_id: str):
    ann = announcements.get(ann_id)
    if not ann:
        raise HTTPException(404, 'Announcement not found')
    ann.enabled = not ann.enabled
    _save_announcements()
    return RedirectResponse('/', status_code=303)


@app.post('/delete/{ann_id}')
async def delete(ann_id: str):
    if ann_id not in announcements:
        raise HTTPException(404, 'Announcement not found')
    announcements.pop(ann_id)
    _save_announcements()
    return RedirectResponse('/', status_code=303)


@app.get('/api/announcements')
async def api_announcements():
    return [asdict(a) for a in announcements.values()]


@app.get('/health')
async def health():
    return {'ok': True, 'announcements': len(announcements), 'sap_group': SAP_GROUP, 'sap_port': SAP_PORT}
