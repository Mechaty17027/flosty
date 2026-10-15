from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from mechahub.core.cash_registers import (
    CHECKED_DATE, MANUFACTURERS, MAX_BODY, MAX_CASE_FILE, MAX_IMPORT_FILE,
    MODELS, CaseStore, read_case_text, search_models,
)


class CatalogueTests(unittest.TestCase):
    def test_all_requested_brands_have_dated_primary_sources(self):
        allowed = {'unisystem.ua', 'exellio.com.ua', 'www.exellio.com.ua',
                   'www.ics-market.com.ua', 'gera.com.ua'}
        self.assertEqual(set(MANUFACTURERS), {model.manufacturer for model in MODELS})
        self.assertEqual(len(MODELS), len({model.key for model in MODELS}))
        for model in MODELS:
            self.assertEqual(model.checked_date, CHECKED_DATE)
            self.assertTrue(model.facts)
            self.assertTrue(model.sources)
            for source in model.sources:
                parsed = urlsplit(source.url)
                self.assertEqual(parsed.scheme, 'https')
                self.assertIn(parsed.hostname, allowed)

    def test_combined_search_and_brand_filter(self):
        found = search_models('ethernet', 'Unisystem')
        self.assertEqual({model.name for model in found}, {'MINI-T 51.01', 'MINI-T 61.01', 'МІНІ-ФП54.01'})
        self.assertFalse(search_models('ethernet', 'невідомий'))
        self.assertFalse(search_models('модель якої немає'))

    def test_ukrainian_names_and_common_brand_spelling(self):
        self.assertEqual(len(search_models('exelio')), 2)
        self.assertEqual(len(search_models('екселліо')), 2)
        self.assertEqual(search_models('IKC-M510')[0].key, 'iks-m510')
        self.assertEqual(search_models('ІКС-М510')[0].key, 'iks-m510')
        self.assertEqual(search_models('MINI-T400')[0].key, 'mini-t400')
        self.assertTrue(all(model.manufacturer == 'GERA / MG' for model in search_models('гера')))

    def test_requested_registrars_and_short_names_select_the_correct_modification(self):
        for query, key in (('міні-фп54', 'mini-fp54'), ('мініфп54', 'mini-fp54'),
                           ('mini fp54', 'mini-fp54'), ('ікс810', 'iks-e810t'),
                           ('ІКС-Е810Т', 'iks-e810t'), ('IKC-E810T', 'iks-e810t')):
            with self.subTest(query=query):
                self.assertEqual([model.key for model in search_models(query)], [key])

    def test_every_model_has_an_offline_photo_matching_its_official_source_and_hash(self):
        folder = Path(__file__).resolve().parents[1] / 'assets' / 'cash_registers'
        records = json.loads((folder / 'SOURCES.json').read_text(encoding='utf-8'))
        metadata = {record['key']: record for record in records}
        self.assertEqual(set(metadata), {model.key for model in MODELS})
        for model in MODELS:
            with self.subTest(model=model.key):
                self.assertIsNotNone(model.image)
                self.assertEqual(Path(model.image.filename).name, model.image.filename)
                record = metadata[model.key]
                self.assertEqual(model.image.source.url, record['url'])
                self.assertEqual(model.image.filename, record['filename'])
                payload = (folder / model.image.filename).read_bytes()
                self.assertEqual(len(payload), record['bytes'])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), record['sha256'])

    def test_troubleshooting_is_sourced_scoped_and_searchable(self):
        allowed = {'unisystem.ua', 'www.exellio.com.ua', 'www.ics-market.com.ua', 'gera.com.ua'}
        for model in MODELS:
            with self.subTest(model=model.key):
                self.assertTrue(model.issue_note)
                self.assertTrue(model.reference_notes)
                self.assertGreaterEqual(len(model.issues), 3)
                for issue in model.issues:
                    self.assertTrue(issue.title and issue.meaning and issue.checks and issue.service)
                    self.assertTrue(issue.sources)
                    for source in issue.sources:
                        parsed = urlsplit(source.url)
                        self.assertEqual(parsed.scheme, 'https')
                        self.assertIn(parsed.hostname, allowed)
        self.assertEqual({model.key for model in search_models('x8B')}, {'mg-v545t02', 'mg-n707ts'})

    def test_api_codes_and_initial_checks_cannot_be_confused_with_display_codes(self):
        for model in MODELS:
            if model.manufacturer == 'GERA / MG':
                self.assertIn('API', model.issue_note)
                self.assertTrue(all(issue.code.startswith('API: ') for issue in model.issues))
            elif model.manufacturer == 'Exellio':
                self.assertIn('Первинні перевірки', model.issue_note)
                self.assertTrue(all(not issue.code for issue in model.issues))
        e810 = next(model for model in MODELS if model.key == 'iks-e810t')
        self.assertTrue(all(not issue.code for issue in e810.issues))


class CaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cases.json'
        self.store = CaseStore(self.path)

    def test_add_edit_delete_preserve_other_cases(self):
        first = self.store.save('Немає зв’язку', 'MINI-T 400ME', 'Перевірки й результат')
        second = self.store.save('Стрічка', '', 'Власний випадок')
        changed = self.store.save('Відновив зв’язок', first.model, 'Новий результат', identifier=first.id)
        loaded = self.store.load()
        self.assertEqual(loaded[0], changed)
        self.assertEqual({case.id for case in loaded}, {first.id, second.id})
        self.store.delete(second.id)
        self.assertEqual(self.store.load(), [changed])

    def test_reject_deleted_case_edit(self):
        case = self.store.save('Назва', '', 'Текст')
        self.store.delete(case.id)
        with self.assertRaisesRegex(ValueError, 'уже видалено'):
            self.store.save('Зміни', '', 'Текст', identifier=case.id)
        self.assertFalse(self.store.load())

    def test_invalid_input_does_not_erase_saved_case(self):
        case = self.store.save('Назва', '', 'Текст')
        original = self.path.read_bytes()
        for args in [('', '', 'Текст'), ('Назва', '', ''), ('Назва', '', 'x' * (MAX_BODY + 1)),
                     ('Назва', '\0', 'Текст'), ('Назва', '', None)]:
            with self.assertRaises(ValueError):
                self.store.save(*args, identifier=case.id)
            self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_and_future_files_are_not_overwritten(self):
        for payload in ['broken json', '{"schema":2,"cases":[]}', '{"schema":true,"cases":[]}',
                        '{"schema":1,"cases":[],"future":true}', '{"schema":1.0,"cases":[]}']:
            self.path.write_text(payload, encoding='utf-8')
            with self.assertRaises(ValueError):
                self.store.save('Назва', '', 'Текст')
            self.assertEqual(self.path.read_text(encoding='utf-8'), payload)

    def test_duplicate_and_invalid_entries_rejected(self):
        case = asdict(self.store.save('Назва', '', 'Текст'))
        self.path.write_text(json.dumps({'schema': 1, 'cases': [case, case]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'повторні'):
            self.store.load()
        case['updated_at'] = 'bad date'
        self.path.write_text(json.dumps({'schema': 1, 'cases': [case]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'дата'):
            self.store.load()

    def test_atomic_replace_failure_preserves_data_and_cleans_temp(self):
        self.store.save('Назва', '', 'Текст')
        original = self.path.read_bytes()
        with patch('mechahub.core.cash_registers.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.save('Друга', '', 'Текст')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_case_file_size_limit(self):
        self.path.write_bytes(b' ' * (MAX_CASE_FILE + 1))
        with self.assertRaisesRegex(ValueError, 'завеликий'):
            self.store.load()

    def test_utf8_import_is_plain_text_and_bounded(self):
        text = Path(self.temp.name) / 'import.txt'
        text.write_text('<b>Власний кейс</b>\nРішення', encoding='utf-8-sig')
        self.assertEqual(read_case_text(text), '<b>Власний кейс</b>\nРішення')
        text.write_bytes(b'x' * (MAX_IMPORT_FILE + 1))
        with self.assertRaisesRegex(ValueError, 'завеликий'):
            read_case_text(text)
        text.write_bytes(b'\xff\xfe')
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            read_case_text(text)


if __name__ == '__main__':
    unittest.main()
