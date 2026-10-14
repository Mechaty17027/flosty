"""Network-quality diagnostics without external command downloads or credentials.

Bandwidth is an approximate HTTPS throughput to Cloudflare, not the Ookla score.
Network interface counters are system-wide, not per-program packet loss.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import platform
import re
import socket
import statistics
import subprocess
import threading
import time
from urllib.request import Request, urlopen

import psutil

from mechahub import __version__

SERVER = 'speed.cloudflare.com'
PING_TARGET = '1.1.1.1'
DOWNLOAD_URL = f'https://{SERVER}/__down'
UPLOAD_PATH = '/__up'
# Traffic caps protect users with metered connections; warm-up + limited measurements.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
PHASE_SECONDS = 6.0
HTTP_TIMEOUT = 2.0
PING_POLL_SECONDS = .1


class TestCancelled(Exception):
    def __init__(self, result: SpeedTestResult | None = None):
        super().__init__('Тест зупинено')
        self.result = result


def _check_cancelled(cancelled: threading.Event | None):
    if cancelled is not None and cancelled.is_set():
        raise TestCancelled()


def _stop_ping(process):
    """Reap only the ping process created by this test, including on cancellation."""
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def mbps(byte_count: int, seconds: float) -> float:
    return max(0., float(byte_count)) * 8.0 / max(.001, seconds) / 1_000_000


def jitter_ms(rtts: list[float]) -> float | None:
    """Mean absolute difference between successive ICMP RTTs."""
    if len(rtts) < 2:
        return None
    return round(statistics.mean(abs(b-a) for a,b in zip(rtts, rtts[1:])), 1)


def parse_ping_replies(text: str) -> list[float]:
    """Accept Windows EN/UK/RU and Linux `time=…ms` ICMP reply formats."""
    expressions = [
        re.compile(r'(?:time|час|время)\s*([=<])\s*([\d.,]+)\s*(?:ms|мс)', re.IGNORECASE),
    ]
    result: list[float] = []
    for line in text.splitlines():
        for pattern in expressions:
            match = pattern.search(line)
            if match:
                try:
                    result.append(float(match.group(2).replace(',', '.')) if match.group(1) == '=' else .5)
                except ValueError:
                    pass
                break
    return result


def parse_ping_sent_received(text: str) -> tuple[int,int] | None:
    """Use ping summary if localized format recognizable; otherwise return None."""
    # EN Windows: Sent = 10, Received = 8, Lost = 2
    m = re.search(r'(?:Sent|Надіслано|Отправлено)\s*=\s*(\d+)\s*[,，]\s*'
                  r'(?:Received|Отримано|Получено)\s*=\s*(\d+)', text, re.I)
    if m:
        return int(m.group(1)),int(m.group(2))
    # POSIX: 10 packets transmitted, 9 received
    m = re.search(r'(\d+)\s+packets?\s+transmitted\s*,\s*(\d+)\s+(?:packets?\s+)?received', text, re.I)
    if m:
        return int(m.group(1)),int(m.group(2))
    return None


def ping_quality(target: str = PING_TARGET, count: int = 10, *, cancelled: threading.Event | None = None) -> dict:
    _check_cancelled(cancelled)
    if platform.system() == 'Windows':
        cmd = ['ping','-n',str(count),'-w','800',target]
    else:
        cmd = ['ping','-c',str(count),'-W','1',target]
    process = None
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                   if platform.system() == 'Windows' else 0)
        deadline = time.monotonic() + max(15, count * 2)
        while True:
            _check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd, max(15, count * 2))
            try:
                data, _ = process.communicate(timeout=PING_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                continue
    except TestCancelled:
        if process is not None:
            _stop_ping(process)
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None:
            _stop_ping(process)
        return dict(sent=None, received=None, lost_percent=None, ping_ms=None,
                    jitter_ms=None, error=f'Ping недоступний: {exc}')
    _check_cancelled(cancelled)
    # Windows usually uses OEM codepage, while Linux uses UTF-8.
    try:
        output = data.decode('utf-8' if platform.system() != 'Windows' else 'oem', errors='replace')
    except LookupError:
        output = data.decode(errors='replace')
    replies = parse_ping_replies(output)
    totals = parse_ping_sent_received(output)
    # If localized output cannot be parsed, don't invent a packet-loss percentage.
    if totals is None and not replies:
        return dict(sent=None, received=None, lost_percent=None, ping_ms=None,
                    jitter_ms=None, error='Не вдалося прочитати відповіді ping (локалізація або блокування ICMP).')
    sent, received = totals if totals else (None, len(replies))
    if sent is not None:
        received = min(sent, received)
    return dict(sent=sent, received=received,
                lost_percent=round(100 * (sent - received) / max(1, sent), 1) if sent else None,
                ping_ms=round(statistics.mean(replies),1) if replies else None,
                jitter_ms=jitter_ms(replies), error=None)


def choose_interface() -> str | None:
    """Pick network interface owning default IPv4 route (without sending packets)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((PING_TARGET, 53))
            ip = sock.getsockname()[0]
        addresses = psutil.net_if_addrs()
        for nic, entries in addresses.items():
            if any(getattr(entry, 'family', None) == socket.AF_INET and entry.address == ip for entry in entries):
                return nic
    except (OSError, ValueError):
        pass
    try:
        candidates = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        available = [(nic, counters) for nic,counters in candidates.items()
                     if nic in stats and stats[nic].isup and not nic.lower().startswith('loopback')]
        if available:
            return max(available, key=lambda x: x[1].bytes_sent+x[1].bytes_recv)[0]
    except (OSError, RuntimeError):
        pass
    return None


def nic_snapshot(interface: str | None):
    if not interface:
        return None
    try:
        return psutil.net_io_counters(pernic=True).get(interface)
    except (OSError, RuntimeError):
        return None


def nic_packet_delta(before, after) -> dict:
    keys = ('packets_sent', 'packets_recv', 'errin', 'errout', 'dropin', 'dropout')
    return {key: max(0, int(getattr(after, key) - getattr(before, key)))
            if before is not None and after is not None and hasattr(before,key) and hasattr(after,key) else None
            for key in keys}


def _check_transfer(cancelled: threading.Event, deadline: float):
    _check_cancelled(cancelled)
    if time.perf_counter() >= deadline:
        raise TimeoutError('Досягнуто ліміту часу вимірювання')


def _http_download(amount: int, callback, cancelled: threading.Event, *, deadline: float | None = None) -> tuple[int, float]:
    if not 0 < amount <= MAX_DOWNLOAD_BYTES:
        raise ValueError('Неприпустимий розмір завантаження')
    _check_cancelled(cancelled)
    req = Request(f'{DOWNLOAD_URL}?bytes={amount}&cacheBust={time.monotonic_ns()}',
                  headers={'Cache-Control': 'no-cache', 'User-Agent': f'MechaHub/{__version__}'})
    start = time.perf_counter()
    deadline = deadline if deadline is not None else start + PHASE_SECONDS
    size = 0
    _check_transfer(cancelled, deadline)
    with urlopen(req, timeout=min(HTTP_TIMEOUT, max(.001, deadline-start))) as response:
        _check_transfer(cancelled, deadline)
        if getattr(response, 'status', 200) != 200:
            raise OSError(f'HTTP {response.status} під час завантаження')
        # read1 returns available data rather than waiting to fill the entire buffer.
        # Bound every read even if the server ignores its requested response size.
        read = getattr(response, 'read1', response.read)
        while size < amount:
            _check_transfer(cancelled, deadline)
            chunk = read(min(128*1024, amount-size))
            if not chunk:
                raise OSError('Сервер перервав завантаження раніше очікуваного')
            size += len(chunk)
            callback(size, time.perf_counter()-start)
            _check_cancelled(cancelled)
    return size, time.perf_counter()-start


def _http_upload(amount: int, callback, cancelled: threading.Event, *, deadline: float | None = None) -> tuple[int, float]:
    if not 0 < amount <= MAX_UPLOAD_BYTES:
        raise ValueError('Неприпустимий розмір віддачі')
    _check_cancelled(cancelled)
    payload = bytes(64*1024)
    start = time.perf_counter()
    deadline = deadline if deadline is not None else start + PHASE_SECONDS
    _check_transfer(cancelled, deadline)
    conn = http.client.HTTPSConnection(SERVER, timeout=min(HTTP_TIMEOUT, max(.001, deadline-start)))
    sent = 0
    try:
        conn.putrequest('POST', UPLOAD_PATH)
        conn.putheader('Content-Type','application/octet-stream')
        conn.putheader('Content-Length',str(amount))
        conn.putheader('Cache-Control','no-cache')
        conn.putheader('User-Agent', f'MechaHub/{__version__}')
        _check_transfer(cancelled, deadline)
        conn.endheaders()
        while sent < amount:
            _check_transfer(cancelled, deadline)
            count = min(len(payload), amount-sent)
            conn.send(payload[:count])
            sent += count
            callback(sent, time.perf_counter()-start)
        _check_transfer(cancelled, deadline)
        resp = conn.getresponse()
        _check_cancelled(cancelled)
        # Only acknowledgement status is needed. Never read an unbounded body.
        if not 200 <= resp.status < 300:
            raise OSError(f'HTTP {resp.status} під час віддачі')
        return sent, time.perf_counter()-start
    finally:
        conn.close()


@dataclass
class SpeedTestResult:
    ping: dict
    download_mbps: float | None
    upload_mbps: float | None
    interface: str | None
    counters: dict
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    error_download: str | None = None
    error_upload: str | None = None
    partial_download: bool = False
    partial_upload: bool = False
    cancelled: bool = False


def speed_test(on_stage, on_speed, cancelled: threading.Event) -> SpeedTestResult:
    """Run in background thread; callbacks invoked in background thread.

    Preserve measured traffic and estimates when a request fails or is cancelled.
    Socket calls have short timeouts; DNS lookup remains controlled by the OS.
    """
    _check_cancelled(cancelled)
    nic = choose_interface()
    before = nic_snapshot(nic)
    ping = dict(sent=None, received=None, lost_percent=None, ping_ms=None,
                jitter_ms=None, error=None)
    speeds: dict[str, float | None] = {'download':None, 'upload':None}
    errors: dict[str, str | None] = {'download':None, 'upload':None}
    traffic: dict[str, int] = {'download':0, 'upload':0}
    partial = {'download': False, 'upload': False}

    def result(stopped=False):
        return SpeedTestResult(
            ping=ping, download_mbps=speeds['download'], upload_mbps=speeds['upload'],
            interface=nic, counters=nic_packet_delta(before, nic_snapshot(nic)),
            downloaded_bytes=traffic['download'], uploaded_bytes=traffic['upload'],
            error_download=errors['download'], error_upload=errors['upload'],
            partial_download=partial['download'], partial_upload=partial['upload'],
            cancelled=stopped)

    on_stage('ping')
    try:
        ping = ping_quality(cancelled=cancelled)
        _check_cancelled(cancelled)
    except TestCancelled:
        raise TestCancelled(result(stopped=True)) from None
    for stage, cap, measure, chunks in (
        ('download', MAX_DOWNLOAD_BYTES, _http_download, (2*1024*1024, 8*1024*1024, 16*1024*1024)),
        ('upload', MAX_UPLOAD_BYTES, _http_upload, (1*1024*1024, 4*1024*1024, 8*1024*1024)),
    ):
        if cancelled.is_set():
            raise TestCancelled(result(stopped=True))
        on_stage(stage)
        phase_start = time.perf_counter()
        bytes_done = 0
        # Track individual request timings and choose weighted speed over completed requests.
        elapsed_done = 0.0
        current_bytes = 0
        current_elapsed = 0.0

        def update_estimate():
            measured_bytes = bytes_done + current_bytes
            traffic[stage] = measured_bytes
            if measured_bytes:
                speeds[stage] = round(mbps(measured_bytes, elapsed_done+current_elapsed), 2)

        try:
            index = 0
            while not cancelled.is_set() and bytes_done < cap and time.perf_counter()-phase_start < PHASE_SECONDS:
                chunk_bytes = min(cap-bytes_done, chunks[min(index,len(chunks)-1)])
                completed_before = bytes_done
                def on_chunk(current, elapsed):
                    nonlocal current_bytes, current_elapsed
                    current_bytes = min(chunk_bytes, max(0, current))
                    current_elapsed = max(.001, elapsed)
                    traffic[stage] = completed_before + current_bytes
                    if elapsed > .08:
                        on_speed(stage, mbps(completed_before+current,
                                             max(.001, elapsed_done+elapsed)))
                current_bytes = 0
                current_elapsed = 0.0
                count, elapsed = measure(chunk_bytes,on_chunk,cancelled,
                                         deadline=phase_start+PHASE_SECONDS)
                if not count:
                    raise OSError('Сервер не передав дані')
                bytes_done += count
                elapsed_done += max(.001,elapsed)
                current_bytes = 0
                current_elapsed = 0.0
                traffic[stage] = bytes_done
                on_speed(stage, mbps(bytes_done,elapsed_done))
                index += 1
            if cancelled.is_set():
                raise TestCancelled()
            if bytes_done:
                speeds[stage] = round(mbps(bytes_done,elapsed_done),2)
            else:
                errors[stage] = 'Не отримано вимірювань'
        except TestCancelled:
            update_estimate()
            partial[stage] = bool(current_bytes)
            raise TestCancelled(result(stopped=True)) from None
        except (OSError, ValueError, http.client.HTTPException) as error:
            update_estimate()
            partial[stage] = bool(current_bytes)
            if cancelled.is_set():
                raise TestCancelled(result(stopped=True)) from None
            errors[stage] = str(error)
    if cancelled.is_set():
        raise TestCancelled(result(stopped=True))
    on_stage('done')
    return result()
