"""Small sourced hardware catalogue and local, user-written technical cases."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
import uuid

from mechahub.core.cash_registers_reference import IMAGE_REFERENCES, ISSUE_REFERENCES, MODEL_REFERENCES


CHECKED_DATE = '2026-10-15'
MANUFACTURERS = ('Unisystem', 'Exellio', 'ІКС', 'GERA / MG')


@dataclass(frozen=True)
class Source:
    title: str
    url: str


@dataclass(frozen=True)
class ProductImage:
    filename: str
    source: Source
    credit: str


@dataclass(frozen=True)
class RegisterIssue:
    title: str
    meaning: str
    checks: tuple[str, ...]
    service: str
    sources: tuple[Source, ...]
    code: str = ''


@dataclass(frozen=True)
class RegisterModel:
    key: str
    manufacturer: str
    name: str
    kind: str
    purpose: str
    facts: tuple[tuple[str, str], ...]
    sources: tuple[Source, ...]
    aliases: tuple[str, ...] = ()
    checked_date: str = CHECKED_DATE
    image: ProductImage | None = None
    issues: tuple[RegisterIssue, ...] = ()
    issue_note: str = ''
    reference_notes: tuple[str, ...] = ()


# Short summaries of the linked official product tables; no sales rankings,
# prices, registration eligibility or frequency rankings are inferred here.
MODELS = (
    RegisterModel('mini-t400', 'Unisystem', 'MINI-T 400ME', 'Портативний касовий апарат',
                  'Виїзна торгівля та невеликі торгові точки.',
                  (('Стрічка', '58 мм; рулон до 40 мм'),
                   ('Підключення', '2 × RS-232, USB; вбудований GSM/GPRS'),
                   ('База товарів', '8192 позиції'),
                   ('Особливості', 'Акумулятор, графічний екран 128 × 64, КСЕФ')),
                  (Source('Офіційна сторінка Unisystem', 'https://unisystem.ua/catalog/kassovye-apparaty/kassovyj-apparat-mini-t-400me-mini-t400-s-ksef/'),),
                  ('міні т400', 'MINI T 400МЕ')),
    RegisterModel('mini-t51', 'Unisystem', 'MINI-T 51.01', 'Універсальний касовий апарат',
                  'Стаціонарні точки з обмеженим місцем і виїзна торгівля.',
                  (('Стрічка', '58 мм; рулон до 40 мм'),
                   ('Підключення', 'RS-232, USB, Ethernet; GSM/GPRS — опція'),
                   ('База товарів', '12 288 позицій'),
                   ('Особливості', 'Акумулятор; підтримка сканера, ваг і грошової скриньки')),
                  (Source('Офіційна сторінка Unisystem', 'https://unisystem.ua/catalog/kassovye-apparaty/portativnyj-kassovyj-apparat-mini-t51-01/'),),
                  ('міні т51',)),
    RegisterModel('mini-t61', 'Unisystem', 'MINI-T 61.01', 'Стаціонарний касовий апарат',
                  'Робоче місце касира в магазині або закладі послуг.',
                  (('Стрічка', '58 мм; рулон до 40 мм'),
                   ('Підключення', 'RS-232, USB, Ethernet; GSM/GPRS — опція'),
                   ('База товарів', '12 288 позицій'),
                   ('Особливості', 'Дисплей покупця; 17 програмованих клавіш; акумулятор')),
                  (Source('Офіційна сторінка Unisystem', 'https://unisystem.ua/catalog/kassovye-apparaty/statsionarnyj-kassovyj-apparat-mini-t61-01/'),),
                  ('міні т61',)),
    RegisterModel('mini-fp54', 'Unisystem', 'МІНІ-ФП54.01', 'Портативний фіскальний реєстратор',
                  'Компактне касове місце з ПК, планшетом або торговельною системою.',
                  (('Версія опису', '5401F3; звіряй версію ПЗ та комплектацію свого пристрою'),
                   ('Стрічка', '58 мм; рулон до 40 мм; без автообрізувача'),
                   ('Підключення до касової системи', 'RS-232, USB; Bluetooth — опція'),
                   ('Мережа', 'Ethernet; GSM/GPRS залежно від комплектації'),
                   ('Живлення та екран', 'Акумулятор Li-Pol 7,4 В, 2 А·год; дисплей покупця')),
                  (Source('Офіційна сторінка Unisystem', 'https://unisystem.ua/catalog/fiskalnye-registratory/fiskalnyj-registrator-mini-fp54-01/'),),
                  ('міні-фп54', 'мініфп54', 'mini-fp54', 'mini fp54', 'мини фп54')),
    RegisterModel('exellio-dp15', 'Exellio', 'Exellio DP-15', 'Портативний касовий апарат',
                  'Торгівля й послуги з автономним живленням.',
                  (('Стрічка', '57 мм за таблицею виробника; рулон до 40 мм'),
                   ('Підключення', '2 × RS-232, Ethernet, GPRS'),
                   ('Периферія', 'Сканер, ваги, платіжний термінал; зовнішній дисплей'),
                   ('Особливості', 'Акумулятор; швидке завантаження паперу; до 100 000 товарів')),
                  (Source('Офіційна сторінка Exellio', 'https://www.exellio.com.ua/ua/kassovye-apparaty/eksellio-dp-15/'),),
                  ('екселліо', 'екселіо', 'exelio')),
    RegisterModel('exellio-dp35', 'Exellio', 'Exellio DP-35', 'Касовий апарат',
                  'Касове місце з підключенням торговельної периферії.',
                  (('Стрічка', '57 мм у таблиці; 57,5 мм в описі. Уточнюй за паспортом.'),
                   ('Підключення', 'RS-232, Ethernet; GPRS і USB в таблиці позначено як опції'),
                   ('Периферія', 'Сканер, ваги, платіжний термінал, грошова скринька'),
                   ('Особливості', 'Графічний дисплей; до 100 000 товарів; швидке завантаження паперу')),
                  (Source('Офіційна сторінка Exellio', 'https://www.exellio.com.ua/ua/kassovye-apparaty/eksellio-dp-35/'),),
                  ('екселліо', 'екселіо', 'exelio')),
    RegisterModel('iks-m510', 'ІКС', 'IKC-М510.01', 'Портативний касовий апарат',
                  'Торгівля, виїзна торгівля та сфера послуг.',
                  (('Виробник', 'ІКС-Техно; офіційний каталог — ІКС-МАРКЕТ'),
                   ('Підключення', 'Ethernet; модифікація Ethernet + GPRS'),
                   ('Порти за настановою 1-04', 'RS-232, USB для ПК, USB для периферії, грошова скринька'),
                   ('Живлення за настановою', 'Li-Pol акумулятор 7,4 В, 2 А·год'),
                   ('Стрічка', 'Параметри паперу перевіряй за паспортом своєї модифікації')),
                  (Source('Офіційна сторінка ІКС-МАРКЕТ', 'https://www.ics-market.com.ua/obladnannya/ikc-m510-01/'),
                   Source('Настанова IKC-М510.01, версія 1-04 (PDF)', 'https://www.ics-market.com.ua/wp-content/uploads/2026/04/IKC-M510.01_1-04_Nastanova.pdf')),
                  ('ікс маркет', 'ics', 'iks', 'IKC-M510.01', 'ІКС-М510.01')),
    RegisterModel('iks-e810t', 'ІКС', 'IKC-E810T', 'Стаціонарний фіскальний реєстратор',
                  'Друк чеків у складі касового місця магазину, закладу послуг або харчування.',
                  (('Модифікація', 'E810T; настанова EP-12. Назва «ІКС-810» використовується в пошуку.'),
                   ('Стрічка', '57,5 ± 0,5 або 79,5 ± 0,5 мм; рулон до 83 мм'),
                   ('Друк', 'До 150 мм/с; автоматичний обрізувач'),
                   ('Підключення', 'RS-232 і USB до ПК; Ethernet для передачі даних'),
                   ('Живлення', 'Зовнішній блок 24 В; параметри звіряй за паспортом')),
                  (Source('Офіційна сторінка ІКС-МАРКЕТ', 'https://www.ics-market.com.ua/e810t/'),
                   Source('Настанова IKC-E810T, EP-12 (PDF)', 'https://www.ics-market.com.ua/wp-content/uploads/2026/06/IKC-E810T_Nastanova_EP-12.pdf')),
                  ('ікс810', 'ікс 810', 'ікс-810', 'ікс е810т', 'IKC-Е810T', 'ІКС-Е810Т', 'iks810', 'ics810')),
    RegisterModel('mg-v545t02', 'GERA / MG', 'MG-V545T.02', 'Портативний касовий апарат',
                  'Торгівля та послуги; лінійка MG у каталозі GERA.',
                  (('Стрічка', '57,5 мм; рулон до 40 мм'),
                   ('Підключення', 'Ethernet, 2 × RS-232, USB A/B; GSM/GPRS та Wi-Fi — опції'),
                   ('База товарів', '20 000 позицій'),
                   ('Особливості', 'Акумулятор 2000 мА·год; графічний дисплей; КСЕФ')),
                  (Source('Офіційна сторінка GERA', 'https://gera.com.ua/obladnannya/kasov-aparati/kasoviy-aparat-mg-v545t.02/'),),
                  ('гера', 'MG V545T 02')),
    RegisterModel('mg-n707ts', 'GERA / MG', 'MG-N707TS', 'Фіскальний реєстратор',
                  'Друк чеків у складі касового місця з ПК або POS-системою.',
                  (('Стрічка', '57,5 мм; рулон до 83 мм'),
                   ('Підключення', 'RS-232, USB, Ethernet; у каталозі також зазначено Wi-Fi'),
                   ('Друк', 'До 56 мм/с; автообрізувача немає'),
                   ('Особливості', 'Вбудований дисплей покупця; КСЕФ; швидке завантаження паперу')),
                  (Source('Офіційна сторінка GERA', 'https://gera.com.ua/obladnannya/fskaln-restratori/fskalniy-restrator-mg-n707ts/'),),
                  ('гера', 'MG N707TS')),
)


def _with_reference(model: RegisterModel) -> RegisterModel:
    filename, url, credit = IMAGE_REFERENCES[model.key]
    issues = tuple(RegisterIssue(
        title=entry['title'], meaning=entry['meaning'], checks=tuple(entry['checks']),
        service=entry['service'], sources=tuple(Source(*source) for source in entry['sources']),
        code=entry.get('code', ''),
    ) for entry in ISSUE_REFERENCES.get(model.key, ()))
    reference = MODEL_REFERENCES.get(model.key, {})
    sources = list(model.sources)
    for title, source_url in reference.get('sources', ()):
        if all(source.url != source_url for source in sources):
            sources.append(Source(title, source_url))
    return replace(model, image=ProductImage(filename, Source('Офіційне фото моделі', url), credit),
                   issues=issues, sources=tuple(sources), issue_note=reference.get('issue_note', ''),
                   reference_notes=tuple(reference.get('notes', ())))


MODELS = tuple(_with_reference(model) for model in MODELS)


def _search_text(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    return re.sub(r'[^\w]+', ' ', value).strip()


def search_models(query: str = '', manufacturer: str = '') -> tuple[RegisterModel, ...]:
    """AND-match words, including Ukrainian/Latin names and common brand spelling."""
    terms = _search_text(query).split()
    matches = []
    for model in MODELS:
        if manufacturer and model.manufacturer != manufacturer:
            continue
        text = _search_text(' '.join((model.key, model.name, model.manufacturer, model.kind,
                                     model.purpose, *model.aliases,
                                     *(value for _label, value in model.facts),
                                     *(value for issue in model.issues for value in
                                       (issue.title, issue.code, issue.meaning)))))
        if all(term in text for term in terms):
            matches.append(model)
    return tuple(matches)


MAX_CASES = 1000
MAX_BODY = 30_000
MAX_CASE_FILE = 2 * 1024 * 1024
MAX_IMPORT_FILE = 128 * 1024
_CASE_LOCK = threading.RLock()


@dataclass(frozen=True)
class TechnicalCase:
    id: str
    title: str
    model: str
    body: str
    updated_at: str


def _valid_text(value, name: str, limit: int, *, required=True) -> str:
    if not isinstance(value, str) or '\0' in value or len(value) > limit:
        raise ValueError(f'Некоректне поле «{name}» або перевищено обмеження {limit} символів.')
    value = value.strip()
    if required and not value:
        raise ValueError(f'Заповни поле «{name}».')
    return value


def _case_from_dict(value) -> TechnicalCase:
    if not isinstance(value, dict) or set(value) != {'id', 'title', 'model', 'body', 'updated_at'}:
        raise ValueError('Файл кейсів містить некоректний запис.')
    identifier = value['id']
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise ValueError('Некоректний ідентифікатор кейсу.')
    stamp = _valid_text(value['updated_at'], 'Дата', 40)
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError as error:
        raise ValueError('Некоректна дата кейсу.') from error
    return TechnicalCase(identifier, _valid_text(value['title'], 'Назва', 160),
                         _valid_text(value['model'], 'Модель', 120, required=False),
                         _valid_text(value['body'], 'Текст', MAX_BODY), stamp)


class CaseStore:
    """Atomic local JSON writes. Corrupt/unsupported files are never replaced."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[TechnicalCase]:
        with _CASE_LOCK:
            if self.path.is_symlink():
                raise ValueError('Шлях кейсів має вказувати на звичайний файл.')
            if not self.path.exists():
                return []
            if not self.path.is_file():
                raise ValueError('Шлях кейсів має вказувати на звичайний файл.')
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_CASE_FILE + 1)
            if len(raw) > MAX_CASE_FILE:
                raise ValueError('Файл кейсів завеликий (понад 2 МБ).')
            try:
                value = json.loads(raw.decode('utf-8-sig'))
            except (UnicodeError, ValueError) as error:
                raise ValueError('Не вдалося прочитати файл кейсів. Збережи його копію перед виправленням.') from error
            if (not isinstance(value, dict) or set(value) != {'schema', 'cases'} or
                    type(value.get('schema')) is not int or value['schema'] != 1):
                raise ValueError('Непідтримуваний формат файлу кейсів.')
            entries = value.get('cases')
            if not isinstance(entries, list) or len(entries) > MAX_CASES:
                raise ValueError('Некоректний список кейсів.')
            cases = [_case_from_dict(entry) for entry in entries]
            if len({case.id for case in cases}) != len(cases):
                raise ValueError('Файл містить повторні ідентифікатори кейсів.')
            return sorted(cases, key=lambda case: case.updated_at, reverse=True)

    def save(self, title: str, model: str, body: str, *, identifier: str | None = None) -> TechnicalCase:
        with _CASE_LOCK:
            cases = self.load()
            if identifier is not None and not any(case.id == identifier for case in cases):
                raise ValueError('Цей кейс уже видалено. Створи новий запис.')
            if identifier is None and len(cases) >= MAX_CASES:
                raise ValueError('Досягнуто обмеження 1000 кейсів.')
            case = _case_from_dict({'id': identifier or uuid.uuid4().hex,
                                    'title': title, 'model': model, 'body': body,
                                    'updated_at': datetime.now(timezone.utc).isoformat(timespec='microseconds')})
            cases = [entry for entry in cases if entry.id != case.id]
            cases.insert(0, case)
            self._write(cases)
            return case

    def delete(self, identifier: str) -> None:
        with _CASE_LOCK:
            cases = self.load()
            remaining = [case for case in cases if case.id != identifier]
            if len(remaining) == len(cases):
                raise ValueError('Кейс не знайдено.')
            self._write(remaining)

    def _write(self, cases: list[TechnicalCase]) -> None:
        payload = (json.dumps({'schema': 1, 'cases': [asdict(case) for case in cases]},
                              ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if len(payload) > MAX_CASE_FILE:
            raise ValueError('Загальний розмір кейсів перевищує 2 МБ. Скороти текст або видали зайві записи.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=f'.{self.path.name}.',
                                             suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def read_case_text(path: Path) -> str:
    """Import UTF-8 plain text only; a user reviews it before saving a case."""
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_IMPORT_FILE + 1)
    if len(raw) > MAX_IMPORT_FILE:
        raise ValueError('Текстовий файл завеликий (понад 128 КБ).')
    try:
        text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeError as error:
        raise ValueError('Збережи текстовий файл у кодуванні UTF-8.') from error
    return _valid_text(text, 'Текст', MAX_BODY)
