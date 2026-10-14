"""Conservative non-recursive file organizer: preview first, no deletions."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

CATEGORIES = {
    'Зображення': {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.svg', '.heic', '.ico'},
    'Відео': {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.wmv'},
    'Документи': {'.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.xls', '.xlsx', '.ppt', '.pptx', '.csv'},
    'Музика': {'.mp3', '.wav', '.flac', '.ogg', '.m4a'},
    'Архіви': {'.zip', '.rar', '.7z', '.tar', '.gz'},
    'Програми': {'.exe', '.msi', '.msix', '.bat'},
    'Код': {'.py', '.cpp', '.c', '.h', '.hpp', '.php', '.js', '.ts', '.html', '.css', '.json', '.java', '.sql'},
}
SKIP_SUFFIXES = {'.tmp', '.part', '.crdownload', '.download'}


@dataclass(frozen=True)
class MovePlan:
    source: Path
    destination: Path
    category: str
    source_signature: tuple[int, int, int, int] | None = field(default=None, repr=False, compare=False)


def category_for(path: Path) -> str:
    suffix = path.suffix.lower()
    for category, suffixes in CATEGORIES.items():
        if suffix in suffixes:
            return category
    return 'Інше'


def free_destination(destination: Path, reserved: set[Path]) -> Path:
    candidate = destination
    index = 2
    # exists() alone misses dangling symlinks, which still occupy a name.
    while os.path.lexists(candidate) or candidate in reserved:
        candidate = destination.with_name(f'{destination.stem} ({index}){destination.suffix}')
        index += 1
    return candidate


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _is_link(path: Path) -> bool:
    """Reject symlinks and Windows junctions, including on Python 3.10."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) in {
        getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', -1),
        getattr(stat, 'IO_REPARSE_TAG_SYMLINK', -2),
    }


def _move_without_overwrite(
    source: Path, destination: Path, expected: tuple[int, int, int, int],
) -> None:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or _signature(info) != expected:
        raise OSError('Файл змінився під час переміщення; повтори попередній перегляд.')
    if os.name == 'nt':
        # Windows rename fails when a destination appears concurrently. Unlike
        # shutil.move(), it cannot fall back to a copy that overwrites that file.
        os.rename(source, destination)
    else:
        # POSIX rename would replace a destination. Creating a hard link is
        # exclusive and keeps the file's metadata without copying its contents.
        os.link(source, destination, follow_symlinks=False)
        try:
            linked_info = destination.lstat()
            if (not stat.S_ISREG(linked_info.st_mode)
                    or _signature(linked_info) != expected
                    or _signature(source.lstat()) != expected):
                raise OSError('Файл змінився під час переміщення; повтори попередній перегляд.')
            source.unlink()
        except OSError:
            destination.unlink()
            raise


def plan_organization(folder: Path) -> list[MovePlan]:
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise NotADirectoryError(f'Немає такої папки: {folder}')
    # Freeze the selected directory as an absolute path for a later preview.
    try:
        folder = folder.resolve(strict=True)
    except RuntimeError as error:
        raise OSError(f'Не вдалося визначити шлях папки: {folder}') from error
    result: list[MovePlan] = []
    reserved: set[Path] = set()
    for file in sorted(folder.iterdir(), key=lambda f: f.name.casefold()):
        try:
            info = file.lstat()
        except FileNotFoundError:
            # Downloads can disappear while the directory is being scanned.
            continue
        if not stat.S_ISREG(info.st_mode) or file.suffix.lower() in SKIP_SUFFIXES:
            continue
        category = category_for(file)
        destination = free_destination(folder / category / file.name, reserved)
        reserved.add(destination)
        result.append(MovePlan(file, destination, category, _signature(info)))
    return result


def apply_organization(plan: list[MovePlan]) -> tuple[int, list[str]]:
    moved = 0
    errors: list[str] = []
    for item in plan:
        try:
            # File might have changed since preview: never overwrite anything.
            info = item.source.lstat()
            if not stat.S_ISREG(info.st_mode):
                errors.append(f'{item.source.name}: файл недоступний')
                continue
            if item.source_signature is not None and _signature(info) != item.source_signature:
                errors.append(f'{item.source.name}: файл змінився після перегляду; повтори попередній перегляд')
                continue
            folder = item.source.parent
            target_folder = item.destination.parent
            if (target_folder.parent != folder or target_folder.name != item.category
                    or item.category not in {*CATEGORIES, 'Інше'}):
                errors.append(f'{item.source.name}: ціль має бути категорійною папкою всередині обраної папки')
                continue
            if folder.resolve() != folder or _is_link(folder):
                errors.append(f'{item.source.name}: обрана папка змінилася після перегляду')
                continue
            target_folder.mkdir(exist_ok=True)
            # Check after mkdir as well: a folder can appear after the preview.
            if _is_link(target_folder) or target_folder.resolve().parent != folder:
                errors.append(f'{item.source.name}: цільова папка є посиланням')
                continue
            safe_target = free_destination(item.destination, set())
            collisions: set[Path] = set()
            while True:
                try:
                    _move_without_overwrite(item.source, safe_target, _signature(info))
                    break
                except FileExistsError:
                    # A concurrent writer claimed this name after the check.
                    # Reserve it even if that writer immediately removes it.
                    collisions.add(safe_target)
                    safe_target = free_destination(item.destination, collisions)
            moved += 1
        except (OSError, RuntimeError) as error:
            errors.append(f'{item.source.name}: {error}')
    return moved, errors
