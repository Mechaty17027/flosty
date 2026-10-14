"""Local code projects and explicit process arguments; no shell or installer."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from uuid import uuid4

from mechahub.core.settings import config_file

MAX_CODE_BYTES = 4 * 1024 * 1024
MAX_PROFILE_BYTES = 64 * 1024
MAX_LANGUAGES = 20


class CodeWorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeTool:
    executable: Path
    arguments: tuple[str, ...] = ()
    label: str = ''


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    extension: str
    run: tuple[str, ...]
    compile: tuple[str, ...] | None = None


DEFAULT_PROFILES = (
    LanguageProfile('Python', '.py', ('{python}', '{file}')),
    LanguageProfile('C++', '.cpp', ('{output}',),
                    ('{compiler}', '-std=c++17', '-x', 'c++', '{file}', '-o', '{output}')),
)


def command_working_directory(profile: LanguageProfile, source: Path,
                              run_command: list[str], phase: str) -> Path:
    """Keep the built-in MinGW linker output relative to its Unicode-aware cwd.

    Runtime and custom-language compilation keep the source directory. Compare
    the complete profile so a user-defined C++ command is never rewritten.
    """
    profile = validate_profile(profile)
    if phase not in {'compile', 'run'}:
        raise CodeWorkspaceError('Невідомий етап виконання коду.')
    if phase == 'compile' and profile == DEFAULT_PROFILES[1]:
        return Path(run_command[0]).parent
    return Path(source).parent


def _executable(value: str | Path | None, *, python: bool = False) -> Path | None:
    if not value:
        return None
    try:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            return None
        path = path.resolve(strict=True)
        if python and ('windowsapps' in {part.lower() for part in path.parts}
                       or path.name.lower() == 'mechahub.exe'
                       or (getattr(sys, 'frozen', False) and path == Path(sys.executable).resolve())):
            return None
        return path
    except (OSError, TypeError, ValueError):
        return None


def _path_tool(name: str, *, python: bool = False) -> Path | None:
    # An absolute candidate avoids Windows' implicit current-directory search.
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        try:
            folder = Path(directory.strip().strip('"'))
            if folder.is_absolute():
                result = _executable(shutil.which(str(folder / name)), python=python)
                if result is not None:
                    return result
        except (OSError, ValueError):
            continue
    return None


def find_python(selected: str | Path | None = None, *, bundled_dir: Path | None = None) -> RuntimeTool | None:
    if selected:
        path = _executable(selected, python=True)
        if path is None:
            raise CodeWorkspaceError('Вибери справжній Python або py.exe; MechaHub і псевдонім Microsoft Store не підходять.')
        prefix = ('-3', '-u', '-X', 'utf8') if path.name.lower() in {'py.exe', 'py'} else ('-u', '-X', 'utf8')
        return RuntimeTool(path, prefix, 'Вибраний Python')
    folders = [bundled_dir] if bundled_dir is not None else [
        Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2])) / 'python_runtime',
        Path(__file__).resolve().parents[2] / 'build' / 'python_runtime',
    ]
    for folder in folders:
        if folder is None:
            continue
        path = _executable(folder / 'python.exe', python=True)
        launcher = folder / 'run_user_code.py'
        if path is not None and launcher.is_file():
            return RuntimeTool(path, ('-u', '-X', 'utf8', str(launcher.resolve())), 'Вбудований Python')
    if not getattr(sys, 'frozen', False):
        path = _executable(sys.executable, python=True)
        if path is not None:
            return RuntimeTool(path, ('-u', '-X', 'utf8'), 'Python поточного середовища')
    for name in ('py.exe', 'python.exe', 'python3.exe', 'python3', 'python'):
        path = _path_tool(name, python=True)
        if path is not None:
            prefix = ('-3', '-u', '-X', 'utf8') if path.name.lower() in {'py.exe', 'py'} else ('-u', '-X', 'utf8')
            return RuntimeTool(path, prefix, 'Установлений Python')
    return None


def find_compiler(selected: str | Path | None = None) -> RuntimeTool | None:
    if selected:
        path = _executable(selected)
        if path is None:
            raise CodeWorkspaceError('Файл компілятора недоступний. Вибери встановлений g++ або clang++.')
        return RuntimeTool(path, label='Вибраний компілятор')
    for name in ('g++.exe', 'clang++.exe', 'g++', 'clang++'):
        path = _path_tool(name)
        if path is not None:
            return RuntimeTool(path, label=path.name)
    return None


def validate_profile(profile: LanguageProfile) -> LanguageProfile:
    if (not isinstance(profile.name, str) or not 1 <= len(profile.name.strip()) <= 48
            or any(ord(char) < 32 for char in profile.name)):
        raise CodeWorkspaceError('Назва мови має містити від 1 до 48 символів.')
    if not isinstance(profile.extension, str) or not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', profile.extension):
        raise CodeWorkspaceError('Розширення має вигляд .py, .cpp або .js.')
    for commands in (profile.run, profile.compile):
        if commands is None:
            continue
        if not isinstance(commands, (list, tuple)) or not 1 <= len(commands) <= 32:
            raise CodeWorkspaceError('Команда має бути масивом від 1 до 32 аргументів.')
        for index, argument in enumerate(commands):
            if not isinstance(argument, str) or len(argument) > 2048 or '\0' in argument:
                raise CodeWorkspaceError('Аргументи мають бути текстом до 2048 символів без нульового символу.')
            for placeholder in re.findall(r'\{([^{}]+)\}', argument):
                if placeholder not in {'file', 'output', 'folder', 'python', 'compiler'}:
                    raise CodeWorkspaceError('Дозволені шаблони {file}, {folder}, {output}.')
                if placeholder in {'python', 'compiler'} and (index != 0 or argument != '{' + placeholder + '}'):
                    raise CodeWorkspaceError('Інтерпретатор або компілятор має стояти на початку команди.')
        if commands[0] not in {'{python}', '{compiler}', '{output}'} and not Path(commands[0]).is_absolute():
            raise CodeWorkspaceError('Першим аргументом має бути абсолютний шлях до вибраної програми.')
        if any(value in commands[1:] for value in ('{python}', '{compiler}')):
            raise CodeWorkspaceError('Інтерпретатор або компілятор має стояти на початку команди.')
    if profile.run is None or (profile.run[0] == '{output}' and not profile.compile):
        raise CodeWorkspaceError('Потрібна команда запуску; {output} потребує команди компіляції.')
    return LanguageProfile(profile.name.strip(), profile.extension.lower(), tuple(profile.run),
                           None if profile.compile is None else tuple(profile.compile))


def _atomic_text(path: Path, text: str, *, limit: int):
    if not isinstance(text, str) or '\0' in text or len(text.encode('utf-8')) > limit:
        raise CodeWorkspaceError('Файл надто великий або містить нульовий символ.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=path.parent,
                                         prefix='.mechahub-code-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise CodeWorkspaceError(f'Не вдалося зберегти файл: {error}') from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CodeWorkspace:
    def __init__(self, root: Path | None = None, settings_path: Path | None = None):
        self.root = Path(root or config_file().with_name('coding')).resolve()
        self.settings_path = Path(settings_path or self.root / 'languages.json')
        self.profiles = list(DEFAULT_PROFILES)
        self.python_executable = ''
        self.compiler_executable = ''
        self.load_error = ''
        if self.settings_path.exists():
            try:
                self._load()
            except (OSError, ValueError, UnicodeError) as error:
                self.load_error = 'Налаштування мов не прочитані: ' + str(error)

    def _load(self):
        if self.settings_path.stat().st_size > MAX_PROFILE_BYTES:
            raise CodeWorkspaceError('Файл налаштувань мов перевищує 64 КіБ.')
        data = json.loads(self.settings_path.read_text(encoding='utf-8-sig'))
        if (not isinstance(data, dict) or set(data) != {'version', 'languages', 'python_executable', 'compiler_executable'}
                or type(data['version']) is not int or data['version'] != 1):
            raise CodeWorkspaceError('Невідомий формат налаштувань мов.')
        if not isinstance(data['languages'], list) or not 2 <= len(data['languages']) <= MAX_LANGUAGES:
            raise CodeWorkspaceError('Некоректний список мов.')
        profiles = []
        for entry in data['languages']:
            if not isinstance(entry, dict) or set(entry) != {'name', 'extension', 'run', 'compile'}:
                raise CodeWorkspaceError('Некоректний профіль мови.')
            profiles.append(validate_profile(LanguageProfile(**entry)))
        if len({profile.name.casefold() for profile in profiles}) != len(profiles):
            raise CodeWorkspaceError('Назви мов мають бути різними.')
        if not {'Python', 'C++'} <= {profile.name for profile in profiles}:
            raise CodeWorkspaceError('Вбудовані профілі Python та C++ потрібні.')
        for field in ('python_executable', 'compiler_executable'):
            if not isinstance(data[field], str) or len(data[field]) > 4096 or '\0' in data[field]:
                raise CodeWorkspaceError('Шлях інструмента некоректний.')
        self.profiles = profiles
        self.python_executable = data['python_executable']
        self.compiler_executable = data['compiler_executable']

    def save_settings(self):
        profiles = [validate_profile(profile) for profile in self.profiles]
        if len(profiles) > MAX_LANGUAGES or len({item.name.casefold() for item in profiles}) != len(profiles):
            raise CodeWorkspaceError('Можна зберегти до 20 мов із різними назвами.')
        data = dict(version=1, languages=[dict(name=item.name, extension=item.extension,
                                             run=list(item.run), compile=None if item.compile is None else list(item.compile))
                                        for item in profiles], python_executable=self.python_executable,
                    compiler_executable=self.compiler_executable)
        _atomic_text(self.settings_path, json.dumps(data, ensure_ascii=False, indent=2) + '\n', limit=MAX_PROFILE_BYTES)

    def add_language(self, profile: LanguageProfile):
        profile = validate_profile(profile)
        if any(item.name.casefold() == profile.name.casefold() for item in self.profiles):
            raise CodeWorkspaceError('Мова з такою назвою вже є.')
        previous = list(self.profiles)
        self.profiles.append(profile)
        try:
            self.save_settings()
        except Exception:
            self.profiles = previous
            raise

    def create_project(self, profile: LanguageProfile, name: str = 'Мій проєкт') -> Path:
        profile = validate_profile(profile)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise CodeWorkspaceError('Назва проєкту має містити від 1 до 80 символів.')
        slug = re.sub(r'[^\w-]+', '-', name.strip(), flags=re.UNICODE).strip('-')[:60] or 'project'
        folder = self.root / 'projects' / f'{slug}-{uuid4().hex[:8]}'
        folder.mkdir(parents=True, exist_ok=False)
        path = folder / ('main' + profile.extension)
        content = ("print('Привіт, MechaHub!')\n" if profile.name == 'Python' else
                   '#include <iostream>\n\nint main() {\n    std::cout << "Hello, MechaHub!\\n";\n    return 0;\n}\n'
                   if profile.name == 'C++' else '')
        self.save_file(path, content)
        return path

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            with Path(path).open('rb') as source:
                payload = source.read(MAX_CODE_BYTES + 1)
            if len(payload) > MAX_CODE_BYTES:
                raise CodeWorkspaceError('Код перевищує 4 МіБ.')
            result = payload.decode('utf-8-sig')
            if '\0' in result:
                raise CodeWorkspaceError('Це двійковий файл, його не можна відкрити як код.')
            return result
        except (OSError, UnicodeError) as error:
            raise CodeWorkspaceError(f'Не вдалося відкрити UTF-8 файл: {error}') from error

    @staticmethod
    def save_file(path: Path, code: str):
        _atomic_text(Path(path), code, limit=MAX_CODE_BYTES)

    def build_commands(self, profile: LanguageProfile, path: Path, *,
                       python: RuntimeTool | None = None, compiler: RuntimeTool | None = None) -> tuple[list[str] | None, list[str]]:
        profile = validate_profile(profile)
        path = Path(path).resolve(strict=True)
        output_folder = self.root / 'builds' / hashlib.sha256(str(path).encode()).hexdigest()[:16]
        output = output_folder / ('program.exe' if os.name == 'nt' else 'program')
        values = {'file': str(path), 'folder': str(path.parent), 'output': str(output)}

        def expand(command):
            prefix = []
            if command[0] == '{python}':
                tool = python or find_python(self.python_executable or None)
                if tool is None:
                    raise CodeWorkspaceError('Python не знайдено. Вибери встановлений інтерпретатор через «Інструмент…».')
                prefix = [str(tool.executable), *tool.arguments]
            elif command[0] == '{compiler}':
                tool = compiler or find_compiler(self.compiler_executable or None)
                if tool is None:
                    raise CodeWorkspaceError('Для C++ потрібен установлений g++ або clang++. Вибери його через «Інструмент…».')
                prefix = [str(tool.executable), *tool.arguments]
            else:
                prefix = [command[0]]
            result = prefix + list(command[1:])
            for index, argument in enumerate(result):
                for key, value in values.items():
                    argument = argument.replace('{' + key + '}', value)
                result[index] = argument
            if result[0] != str(output) and _executable(result[0]) is None:
                raise CodeWorkspaceError('Виконуваний файл інструмента відсутній. Перевір налаштування мови.')
            return result

        compile_command = expand(profile.compile) if profile.compile else None
        run_command = expand(profile.run)
        if compile_command is not None:
            output_folder.mkdir(parents=True, exist_ok=True)
            if profile == DEFAULT_PROFILES[1]:
                # Some MinGW ld builds convert their output argument through
                # the Windows ANSI code page. Keep that argument ASCII, and
                # let QProcess set the real output directory through UTF-16.
                compile_command[-1] = output.name
        return compile_command, run_command
