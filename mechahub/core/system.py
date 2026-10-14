"""System statistics with graceful fallbacks on restricted Windows machines."""
from __future__ import annotations

import platform
import time
from pathlib import Path

import psutil


def format_bytes(n: float) -> str:
    number = max(0.0, float(n))
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if number < 1024 or unit == 'ТБ':
            return f'{number:.0f} {unit}' if unit == 'Б' else f'{number:.1f} {unit}'
        number /= 1024
    return '0 Б'


def format_uptime(seconds: float) -> str:
    total_minutes = max(0, int(seconds)) // 60
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    return f'{days} д {hours} год {minutes} хв' if days else f'{hours} год {minutes} хв'


class SystemMonitor:
    def __init__(self):
        self._network_last = None
        self._time_last = None
        psutil.cpu_percent(interval=None)  # Prime the nonblocking CPU measurement.

    def snapshot(self) -> dict:
        cpu_percent = psutil.cpu_percent(interval=None)
        virtual_mem = psutil.virtual_memory()
        disk_root = Path.home().anchor or '/'
        disk = psutil.disk_usage(disk_root)
        now = time.monotonic()
        net = psutil.net_io_counters()
        down_rate = up_rate = 0.0
        if net and self._network_last and self._time_last is not None:
            delta = max(0.001, now - self._time_last)
            down_rate = max(0, net.bytes_recv - self._network_last.bytes_recv) / delta
            up_rate = max(0, net.bytes_sent - self._network_last.bytes_sent) / delta
        self._network_last, self._time_last = net, now
        return {
            'cpu_percent': cpu_percent,
            'memory_percent': virtual_mem.percent,
            'memory_used': virtual_mem.used,
            'memory_total': virtual_mem.total,
            'disk_percent': disk.percent,
            'disk_used': disk.used,
            'disk_total': disk.total,
            'disk_root': disk_root,
            'download_rate': down_rate,
            'upload_rate': up_rate,
            'uptime': format_uptime(time.time() - psutil.boot_time()),
            'os_name': f'{platform.system()} {platform.release()}',
            'cpu_cores': psutil.cpu_count(logical=False) or '?',
            'cpu_threads': psutil.cpu_count(logical=True) or '?',
        }


def memory_heavy_processes(max_items: int = 5) -> list[tuple[str, int]]:
    entries = []
    for process in psutil.process_iter(['name', 'memory_info']):
        try:
            info = process.info
            used = info.get('memory_info')
            if used:
                entries.append((info.get('name') or f'PID {process.pid}', used.rss))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
    return sorted(entries, key=lambda item: item[1], reverse=True)[:max_items]
