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
- Windows EXE с включённым Python runtime и FFmpeg.

## Готовая Windows-версия

Готовый файл находится в `dist/YummyAnimeManager.exe`.

Это сборка PyInstaller версии 4.2.1. Для объединения озвучек нужен MKVToolNix.
Настройки хранятся в `%USERPROFILE%\\.yummy_anime_manager` и не входят в репозиторий.

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

Команда создаёт ту же PyInstaller-сборку исходного GUI, что находится в `dist/`.

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
python -m unittest discover -s tests -v
python test_resolvers.py
python test_plexmatch.py
```

## Важное замечание

Приложение должно использоваться только для контента, который пользователь имеет
право сохранять. Оно не предназначено для обхода DRM.

## Third-party / licensing

См. [NOTICE.md](NOTICE.md). Отдельная лицензия проекта намеренно не добавлена до
проверки прав на resolver-код, основанный на поведении/референсах сторонних
проектов.
