# Підготовка MechaHub до GitHub

Публічний репозиторій проєкту: [Mechaty17027/flosty](https://github.com/Mechaty17027/flosty). Проєкт містить сценарії перевірки, Windows-збірки й публікації релізу. Вони запускаються на GitHub після завантаження коду; готовність релізу підтверджує успішний сценарій у Actions.

## Що підготовлено

- `.github/workflows/check.yml`: перевірка залежностей і тести на Windows із Python 3.12 при змінах у `main` або pull request.
- `.github/workflows/release.yml`: після зміни номера версії в `main`, надсилання тега `vX.Y.Z` або ручного запуску через Actions перевіряє номер програми, виконує тести, збирає `MechaHub.exe` і публікує його разом з `update.json`. Якщо реліз із цим тегом уже існує, його файли зберігаються й повторна збірка пропускається.
- `tools/make_update_manifest.py`: обчислює SHA-256 та фактичний розмір готового EXE; створює посилання на цей конкретний реліз.
- `.gitignore`: виключає локальне середовище, результати збірки, кеші та знімки інтерфейсу.
- `.gitattributes`: зберігає Windows BAT із CRLF, а текстовий код — із LF.

Сценарії використовують офіційні [checkout](https://github.com/actions/checkout) і [setup-python](https://github.com/actions/setup-python); публікація виконується через [GitHub CLI release create](https://cli.github.com/manual/gh_release_create).

## Перший репозиторій і реліз

1. Відкрий публічний репозиторій [Mechaty17027/flosty](https://github.com/Mechaty17027/flosty). Публічність дозволяє програмі отримувати оновлення без особистого токена GitHub.
2. Завантаж у гілку `main` вміст саме папки `MechaHub_v0.3.0`: `main.py`, `mechahub`, `assets`, `tests`, `tools`, `.github` та інші вихідні файли. `.venv`, `build`, `dist` і `artifacts` не надсилай у Git.
3. Переконайся, що GitHub Actions дозволені. Перший push із файлом версії й сценарієм релізу запускає `Check MechaHub` та `Publish Windows release`.
4. Дочекайся успішного завершення сценаріїв. `Publish Windows release` визначить версію `0.5.0`, створить тег `v0.5.0` на коміті цієї збірки й опублікує реліз із вкладеннями `MechaHub.exe`, `MechaHubSetup.exe` та `update.json`. Ручний запуск доступний за інструкцією нижче.
5. У MechaHub відкрий «Оновлення» → «Перевірка онлайн». Адреса джерела цього проєкту:

```text
https://github.com/Mechaty17027/flosty/releases/latest/download/update.json
```

Після налаштування джерела програма може перевіряти нові версії при запуску й завантажувати їх автоматично. Поки репозиторій не має опублікованого релізу, ця адреса ще не працюватиме.

## Автоматична публікація наступної версії

Підготуй зміни програми, історію змін і `RELEASE_NOTES_UK.md`, потім онови `__version__` у `mechahub/__init__.py`, наприклад до `0.5.1`. Надішли всі ці зміни разом у `main`. Сценарій отримає тег `v0.5.1` із номера версії, перевірить код, збере EXE й опублікує реліз на цьому конкретному коміті.

Автоматична публікація реагує на `mechahub/__init__.py` і `.github/workflows/release.yml`. Для змін цих файлів зі вже опублікованою версією сценарій повідомить, що реліз існує, і пропустить повторну збірку та завантаження вкладень. Перевірка `Check MechaHub` продовжує виконуватися для змін коду в `main`.

Надсилання окремого тега `vX.Y.Z` також запускає реліз: GitHub [не застосовує фільтр шляхів до push тегів](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore). Для ручної публікації використовуй варіант нижче.

## Ручний запуск через браузер

Після завантаження коду в основну гілку `main` і успішної перевірки `Check MechaHub` відкрий у репозиторії **Actions → Publish Windows release → Run workflow**. Вибери гілку `main` і введи тег `v0.5.0`, потім натисни **Run workflow**. Кнопка ручного запуску доступна, коли файл сценарію вже є в основній гілці; це поведінка [workflow_dispatch у GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch).

Значення тега має точно дорівнювати `v` плюс `__version__` із `mechahub/__init__.py`. Наприклад, для `__version__ = '0.5.0'` потрібен `v0.5.0`. Перевірка номера зупинить сценарій, якщо вони не збігаються.

GitHub виконає тести, збере EXE, створить відсутній тег на конкретному коміті цього запуску й опублікує усі три файли. Вручну завантажувати EXE та JSON у реліз не потрібно. Якщо реліз із цим тегом уже існує, сценарій пропустить повторну збірку й збереже його вкладення. Для наступної версії онови номер і release notes та введи новий тег.

Для запуску після push тега сценарій перевіряє, що цей тег уже є на GitHub. При push версії в `main` або ручному запуску новий тег створює GitHub CLI з `--target`, який указує на SHA перевіреного коміту. Тег, створений сценарієм через `GITHUB_TOKEN`, не запускає другий `push` workflow; GitHub описує це правило в [документації про запуск сценаріїв](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

## Приклад команд Git

Команди використовують репозиторій цього проєкту. Перед push підготуй код і release notes для версії, зазначеної в `mechahub/__init__.py`: зміна версії запускає публікацію автоматично.

```powershell
git init -b main
git add .
git commit -m "Prepare MechaHub 0.5.0"
git remote add origin https://github.com/Mechaty17027/flosty.git
git push -u origin main
```

Для наступного релізу онови `mechahub/__init__.py`, історію змін і `RELEASE_NOTES_UK.md`, перевір застосунок та надішли зміни в `main`. За потреби можна надіслати новий тег або запустити сценарій вручну з тегом, що точно збігається з версією. Невідповідний тег зупинить сценарій до публікації.

## Формат джерела оновлень

`update.json` створюється зі справжнього бінарного файла. Не заповнюй контрольну суму вручну й не підмінюй її прикладом.

```powershell
python tools/make_update_manifest.py --repository Mechaty17027/flosty --tag v0.5.0 --artifact dist/MechaHub.exe --output dist/update.json --notes-file RELEASE_NOTES_UK.md
```

Поля: `version`, `download_url`, `sha256`, `size`, `notes`. Посилання на EXE вказує на конкретний тег; посилання джерела через `latest` отримує опис найновішого релізу. Перед завершенням завантаження MechaHub перевіряє розмір і SHA-256. Контрольна сума підтверджує відповідність файла опису релізу; вона не є цифровим підписом Windows.

Публікація використовує `GITHUB_TOKEN`, наданий самим GitHub Actions. Особисті токени не потрібно записувати у код або конфігурацію програми.
