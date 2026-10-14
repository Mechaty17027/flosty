"""Ping-based network diagnostics for MechaHub."""
from __future__ import annotations

import platform
import re
import subprocess
import time


LOSS_PATTERNS = [
    re.compile(r'(\d+)\s*%\s*loss', re.IGNORECASE),
    re.compile(r'(\d+)\s*%\s*втрат', re.IGNORECASE),
]
LATENCY_PATTERNS = [
    re.compile(r'Average\s*=\s*(\d+)ms', re.IGNORECASE),
    re.compile(r'Serednie\s*=\s*(\d+)ms', re.IGNORECASE),
    re.compile(r'Середнє\s*=\s*(\d+)мс', re.IGNORECASE),
    re.compile(r'min/avg/max(?:/mdev)?\s*=\s*[\d.]+/([\d.]+)/', re.IGNORECASE),
]


def _parse_loss(text: str) -> int | None:
    for pattern in LOSS_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _parse_latency(text: str) -> float | None:
    for pattern in LATENCY_PATTERNS:
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    samples = [float(value) for value in re.findall(r'time[=<]\s*([\d.]+)\s*ms', text, re.IGNORECASE)]
    return round(sum(samples) / len(samples), 1) if samples else None


def run_ping_test(host: str = '1.1.1.1', count: int = 8, timeout_ms: int = 1000) -> dict[str, object]:
    system = platform.system().lower()
    if system == 'windows':
        cmd = ['ping', '-n', str(count), '-w', str(timeout_ms), host]
    else:
        seconds = max(1, round(timeout_ms / 1000))
        cmd = ['ping', '-c', str(count), '-W', str(seconds), host]
    started = time.perf_counter()
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=max(10, count * 2 + 2), errors='ignore')
    elapsed = round(time.perf_counter() - started, 2)
    text = (completed.stdout or '') + '\n' + (completed.stderr or '')
    loss = _parse_loss(text)
    latency = _parse_latency(text)
    online = completed.returncode == 0 or (loss is not None and loss < 100)
    return {
        'host': host,
        'sent': count,
        'packet_loss': loss,
        'avg_latency_ms': latency,
        'success': online,
        'elapsed_sec': elapsed,
        'raw_output': text.strip(),
    }
