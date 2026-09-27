# YummyAnime Manager

Windows-приложение для загрузки аниме по ссылке YummyAnime с выбором плеера,
озвучек, сезона и реально доступного качества. Поддерживает multi-audio MKV,
Plex-friendly naming и `.plexmatch`.

## Возможности

- URL YummyAnime → список плееров, озвучек и серий;
- динамическое определение реально доступных качеств;
- источники: CVH, Kodik, Aksor, Sibnet, Rutube, VK Video, Zedfilm и прямые media URL;
- Alloha через self-hosted resolver из официального YummyAnime Lampa plugin;
- HLS/DASH через FFmpeg без перекодирования;
- несколько озвучек → один MKV через MKVToolNix;
- Plex-структура `Название / Season XX / Название - SxxEyy.mkv`;
- автоматический `.plexmatch`;
- отдельный прогресс текущей серии и всей очереди;
- Windows EXE, который сам разворачивает runtime и проверяет внешние компоненты.

## Готовая Windows-версия

Готовый файл находится в `dist/YummyAnimeManager.exe`.

При первом запуске EXE разворачивает runtime в `%LOCALAPPDATA%\\YummyAnimeManager`,
проверяет FFmpeg и MKVToolNix и при необходимости предлагает установку. Настройки
хранятся в `%USERPROFILE%\\.yummy_anime_manager` и не входят в репозиторий.

## Запуск из исходников

Требуется Python 3.11+.

```bat
install.bat
run.bat
```

Или вручную:

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python main.py
```

## Сборка простого PyInstaller EXE

```bat
build_exe.bat
```

Это обычная PyInstaller-сборка исходного GUI. Готовый bootstrap EXE из `dist/`
использует отдельный bootstrap-механизм, описанный в `docs/EXE_README.txt`.

## Alloha

Для Alloha требуется официальный self-hosted resolver:

```bat
install_alloha_resolver.bat
start_alloha_resolver.bat
```

Нужен Node.js LTS. По умолчанию приложение ожидает resolver по адресу
`http://127.0.0.1:8790`.

## Токен YummyAnime

Используется только публичный application token в заголовке `X-Application`.
Приватный токен приложение не запрашивает и не хранит.

## Тесты

```bash
python tests/test_resolvers.py
python tests/test_plexmatch.py
```

## Обновления

Bootstrap EXE уже умеет проверять HTTPS JSON manifest, загружать новую версию,
проверять SHA-256 и заменять себя. Автообновление настроено на `update.json` в этом публичном репозитории. При запуске
bootstrap EXE сравнивает версию, скачивает новый `dist/YummyAnimeManager.exe`,
проверяет SHA-256 и перезапускается.

## Важное замечание

Приложение должно использоваться только для контента, который пользователь имеет
право сохранять. Оно не предназначено для обхода DRM.

## Third-party / licensing

См. [NOTICE.md](NOTICE.md). Отдельная лицензия проекта намеренно не добавлена до
проверки прав на resolver-код, основанный на поведении/референсах сторонних
проектов.
