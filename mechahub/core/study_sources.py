"""Curated primary preparation links, checked on 2026-10-14.

Only link to publishers' original pages; do not redistribute their lessons.
The latest published UCEQA subject specification checked here is NMT 2026.
Future exam rules must be checked on the official portal before relying on them.
"""
from __future__ import annotations

from dataclasses import dataclass


SOURCES_VERIFIED_ON = '2026-10-14'
SUBJECTS = {
    'math': 'Математика',
    'history': 'Історія України',
    'english': 'Англійська мова',
    'ukrainian': 'Українська мова',
}


@dataclass(frozen=True)
class StudySource:
    subject: str
    title: str
    url: str
    description: str
    provider: str


_SUBJECT_LINKS = (
    ('math', 'Математика. Просто', '7', 'matematyka-2026'),
    ('history', 'Історія України в історіях', '8', 'istoriya-ukrayiny-2026'),
    ('english', 'Ізі ЗНО. Англійська мова', '9', 'inozemni-movy-2026'),
    ('ukrainian', 'Лайфхаки з української мови', '1', 'ukrayinska-mova-2026'),
)

STUDY_SOURCES = tuple(
    source
    for subject, course, course_id, specification in _SUBJECT_LINKS
    for source in (
        StudySource(
            subject, f'iLearn · {course}',
            f'https://ilearn.org.ua/courses/preview/{course_id}',
            'Безоплатний навчальний курс від Освіторії. Для проходження '
            'курсу й збереження прогресу може знадобитися безоплатна реєстрація.',
            'iLearn · Освіторія',
        ),
        StudySource(
            subject, 'iLearn · вебінари, тести й тренажер',
            'https://ilearn.org.ua/',
            'Поточні вебінари, записи та тренувальні завдання. Англійська '
            'мова також є в каталозі курсів і тестах попередніх років.',
            'iLearn · Освіторія',
        ),
        StudySource(
            subject, f'УЦОЯО · {SUBJECTS[subject]}: структура НМТ-2026',
            f'https://testportal.gov.ua/{specification}/',
            'Офіційні вимоги, зміст предметного тесту та оцінювання. '
            'Правила для наступного року перевіряй на сайті УЦОЯО.',
            'Український центр оцінювання якості освіти',
        ),
        StudySource(
            subject, 'УЦОЯО · програми підготовки до НМТ',
            'https://testportal.gov.ua/programy-nmt/',
            'Офіційні програми з предметів: перелік тем, знань і вмінь для підготовки.',
            'Український центр оцінювання якості освіти',
        ),
        StudySource(
            subject, 'УЦОЯО · демонстраційні варіанти НМТ-2026',
            'https://testportal.gov.ua/demonstratsijnyj-variant-nmt-2026/',
            'Офіційні приклади завдань з усіх чотирьох потрібних предметів; '
            'доступні PDF для тренування.',
            'Український центр оцінювання якості освіти',
        ),
    )
)


def sources_for_subject(subject: str) -> tuple[StudySource, ...]:
    """Accept stored subject keys or displayed Ukrainian subject names."""
    key = next((key for key, label in SUBJECTS.items() if label == subject), subject)
    return tuple(source for source in STUDY_SOURCES if source.subject == key)
