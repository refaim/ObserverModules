# Unity Observer Module — План реализации

## Архитектура

```
dll.cpp                    (C, экспорты Observer API)
  → unity_archive.h/cpp    (чистый C++, оркестрация)
    → bridge.h/cpp         (C++/CLI за pimpl, общение с AssetStudio)
      → AssetStudio.dll    (ILRepack-merged: AssetStudioUtility + все зависимости)
      → Texture2DDecoderNative.dll  (нативный C++ декодер текстур, загружается через P/Invoke)
```

Только `bridge.cpp` компилируется с `/clr`. Остальные файлы — чистый нативный C++.

НЕ использует `extractor.h` — объекты Unity десериализуются AssetStudio и экспортируются
с конвертацией формата (Texture2D → PNG, AudioClip → WAV и т.д.), что несовместимо
с chunk-based `decrypt()` моделью.

### Раскладка дистрибутива

```
modules/
├── unity.so                        ← C++/CLI mixed-mode DLL (платформо-зависимый: x86 или x64)
├── observer_user.ini               ← фильтр расширений
└── unity/
    ├── AssetStudio.dll             ← ILRepack-merged сборка (Any CPU, ~10-15 MB)
    └── Texture2DDecoderNative.dll  ← нативный декодер текстур (платформо-зависимый, ~500 KB)
```

### Раскладка репозитория

```
ObserverModules/
├── extern/
│   └── AssetStudio/                     ← git submodule (aelurum/AssetStudio)
├── src/
│   ├── api.h
│   ├── modules/
│   │   └── unity/
│   │       ├── bridge.h                 ← чистый C++ интерфейс (pimpl)
│   │       ├── bridge.cpp               ← C++/CLI реализация (/clr)
│   │       ├── unity_archive.h          ← чистый C++ обёртка архива
│   │       ├── unity_archive.cpp
│   │       ├── dll.cpp                  ← экспорты Observer API (чистый C)
│   │       ├── unity.def                ← экспорты DLL
│   │       └── observer_user.ini        ← фильтр расширений
│   └── tests/
│       ├── unity.cpp                    ← интеграционные тесты
│       └── framework/
└── CMakeLists.txt
```

### CI Pipeline

```
Step 1: git submodule update --init (AssetStudio)
Step 2: dotnet restore extern/AssetStudio
Step 3: dotnet build extern/AssetStudio -c Release -f net472
Step 4: ilrepack /out:AssetStudio.dll AssetStudioUtility.dll <deps...>
Step 5: cmake --preset x64-release && cmake --build build/x64-release
Step 6: cmake --preset x86-release && cmake --build build/x86-release
Step 7: ctest (все тесты)
Step 8: cpack (пакеты x86 + x64 ZIP)
```

### Маппинг форматов экспорта

При листинге файлов каждый Unity-объект показывается с соответствующим расширением.
При извлечении AssetStudio конвертирует в этот формат.

| Тип Unity | Формат экспорта | Расширение | Примечания |
|-----------|----------------|------------|------------|
| Texture2D | PNG | `.png` | GPU-форматы декодируются через Texture2DDecoderNative |
| Sprite | PNG | `.png` | Обрезается по атласу |
| AudioClip | WAV | `.wav` | FSB/FMOD; может потребоваться нативная FMOD-библиотека |
| TextAsset | Сырые байты | `.txt` / `.bytes` | Как есть, расширение из оригинального имени |
| Font | TrueType | `.ttf` / `.otf` | Сырые данные шрифта |
| Shader | Текст | `.shader` | Исходник или дизассемблированный код |
| VideoClip | Ссылка | `.mp4` и т.д. | Обычно внешний .resource файл |
| Mesh | Сырые данные | `.mesh.bytes` | Нет стандартного просмотрщика |
| MonoBehaviour | JSON | `.json` | Сериализованные поля (если доступен type tree) |
| Прочее | Сырые байты | `.bytes` | Фоллбэк для неподдерживаемых типов |

---

## Фаза 0: Подготовка

### 0.1 — Проектирование интерфейса (БЛОКИРУЕТ остальные фазы)

- [ ] **0.1.1** [PROG-A] Спроектировать `bridge.h` — чистый C++ интерфейс с pimpl:
  - Namespace `unity`, класс `bundle` с методами: `try_open`, `format`, `asset_count`, `get_asset`, `extract`
  - Структура `asset_info` (имя, тип, размер экспорта, оригинальный размер)
  - Enum `asset_type` для маппинга типов Unity
  - Свободные функции `init(module_dir_path)` / `shutdown()` — загрузка AssetStudio.dll из подпапки `unity/`
  - Managed-типы не должны утекать наружу
- [ ] **0.1.2** [REVIEW-A] Ревью `bridge.h`
- [ ] **0.1.3** [PROG-A] Исправления по ревью

### 0.2 — Скелет системы сборки (после 0.1.1)

- [ ] **0.2.1** [PROG-B] Добавить aelurum/AssetStudio как git submodule в `extern/AssetStudio`
- [ ] **0.2.2** [PROG-B] Добавить таргет `unity` в `CMakeLists.txt`:
  - Shared library → `unity.so`
  - `bridge.cpp` с `/clr` + `/EHa` (per-file property)
  - Весь модуль с `/MD` (динамический CRT, требование `/clr`)
  - `unity.def` с экспортами `LoadSubModule` / `UnloadSubModule`
- [ ] **0.2.3** [PROG-B] Проверить что скелет компилируется (пустые заглушки)
- [ ] **0.2.4** [REVIEW-B] Ревью CMake
- [ ] **0.2.5** [PROG-B] Исправления по ревью

### 0.3 — Сборка AssetStudio + ILRepack (параллельно с 0.2)

- [ ] **0.3.1** [PROG-B] Создать скрипт `scripts/build_assetstudio.bat`:
  - Сборка AssetStudioUtility под net472
  - ILRepack всех managed-зависимостей в одну `AssetStudio.dll`
  - Сборка `Texture2DDecoderNative.dll` под x86 и x64
  - Копирование артефактов в build output
- [ ] **0.3.2** [PROG-B] Проверить что ILRepack-merged сборка работает: AssetsManager инициализируется, открывает тестовый .assets файл
- [ ] **0.3.3** [REVIEW-B] Ревью скрипта сборки
- [ ] **0.3.4** [PROG-B] Исправления по ревью

---

## Фаза 1: Bridge Layer (C++/CLI ↔ AssetStudio)

Может идти **параллельно** с фазой 2 после финализации `bridge.h`.

### 1.1 — Init/Shutdown

- [ ] **1.1.1** [PROG-A] Тесты + реализация `unity::init()` / `unity::shutdown()`:
  - `GetModuleFileName()` → определение пути к своей DLL
  - Загрузка AssetStudio.dll через `Assembly::LoadFrom()`
  - `AddDllDirectory()` для подпапки `unity/` — чтобы P/Invoke нашёл Texture2DDecoderNative.dll
  - Идемпотентность, безопасность повторных вызовов
- [ ] **1.1.2** [REVIEW-A] Ревью
- [ ] **1.1.3** [PROG-A] Исправления по ревью

### 1.2 — Открытие ассетов (try_open)

- [ ] **1.2.1** [PROG-A] Тесты + реализация `unity::bundle::try_open()`:
  - `AssetsManager` в pimpl через `gcroot<>`
  - `LoadFiles(path)` → перечисление всех `SerializedFile` и их объектов
  - Построение списка `asset_info` из экспортируемых объектов
  - Формирование строки формата ("Unity AssetBundle (LZ4)", "Unity Assets" и т.д.)
  - Невалидный/несуществующий/пустой файл → false без исключений
- [ ] **1.2.2** [REVIEW-A] Ревью
- [ ] **1.2.3** [PROG-A] Исправления по ревью

### 1.3 — Листинг ассетов (asset_count / get_asset)

- [ ] **1.3.1** [PROG-A] Тесты + реализация:
  - Итерация объектов, фильтрация экспортируемых типов
  - Маппинг в `asset_info` с именем + расширение экспорта
  - Дедупликация имён (суффикс `_N` при коллизиях)
  - Вложенные .assets → плоский список
- [ ] **1.3.2** [REVIEW-A] Ревью
- [ ] **1.3.3** [PROG-A] Исправления по ревью

### 1.4 — Извлечение с конвертацией (extract)

- [ ] **1.4.1** [PROG-A] Тесты + реализация:
  - По типу объекта: Texture2D → PNG, Sprite → PNG, AudioClip → WAV, TextAsset → as-is, Font → TTF, Shader → текст, MonoBehaviour → JSON (если есть type tree), прочее → сырые байты
  - Запись чанками с вызовом progress callback
  - Прерывание по callback returning false
- [ ] **1.4.2** [REVIEW-A] Ревью — особое внимание: memory pressure на больших текстурах, disposal потоков, exception safety
- [ ] **1.4.3** [PROG-A] Исправления по ревью

### 1.5 — Деструктор / очистка ресурсов

- [ ] **1.5.1** [PROG-A] Тесты + реализация: dispose gcroot, освобождение AssetsManager, move-семантика
- [ ] **1.5.2** [REVIEW-A] Ревью
- [ ] **1.5.3** [PROG-A] Исправления по ревью

---

## Фаза 2: Слой архива (чистый C++)

Может идти **параллельно** с фазой 1 после финализации `bridge.h`.
Юнит-тесты через мок bridge.

### 2.1 — Мок + Archive Wrapper

- [ ] **2.1.1** [PROG-B] Мок `unity::bundle` для юнит-тестов (настраиваемый список файлов, поведение extract, инъекция ошибок)
- [ ] **2.1.2** [PROG-B] Тесты + реализация `unity_archive` в `unity_archive.h/cpp`:
  - Обёртка над `unity::bundle`
  - Аналогичный паттерн `archive::archive`, но без зависимости от `extractor.h`
  - Конвертация `unity::asset_info` → внутренняя структура файла
  - Трансляция исключений, нормализация путей
- [ ] **2.1.3** [REVIEW-B] Ревью
- [ ] **2.1.4** [PROG-B] Исправления по ревью

---

## Фаза 3: Точки входа DLL (Observer API)

Зависит от стабильного интерфейса фазы 2. Тесты можно писать параллельно с фазами 1+2.

### 3.1 — dll.cpp для Unity-модуля

- [ ] **3.1.1** [PROG-C] Тесты для всех функций Observer API (`OpenStorage`, `CloseStorage`, `PrepareFiles`, `GetItem`, `ExtractItem`, `LoadSubModule`/`UnloadSubModule`)
- [ ] **3.1.2** [PROG-C] Реализация `dll.cpp` по паттерну существующих модулей
- [ ] **3.1.3** [PROG-C] Создать `unity.def` и `observer_user.ini` (расширения: `*.assets`, `*.unity3d`, `*.bundle`, `*.ab`)
- [ ] **3.1.4** [REVIEW-C] Ревью — особое внимание: исключения не должны вылетать из extern "C"
- [ ] **3.1.5** [PROG-C] Исправления по ревью

---

## Фаза 4: Интеграционное тестирование

Зависит от фаз 1, 2, 3.

### 4.1 — End-to-End тесты

- [ ] **4.1.1** [PROG-D] Подготовить тестовые Unity-файлы (< 1 MB):
  - AssetBundle с Texture2D (LZ4)
  - AssetBundle с AudioClip
  - Raw .assets с TextAsset
  - AssetBundle со смешанными типами
  - Битый/пустой файл для проверки ошибок
- [ ] **4.1.2** [PROG-D] Написать `src/tests/unity.cpp` — Catch2 тесты через `test::observer`:
  - Загрузка unity.so, открытие файлов, листинг, извлечение, проверка хешей
- [ ] **4.1.3** [REVIEW-D] Ревью тестов
- [ ] **4.1.4** [PROG-D] Исправления по ревью

### 4.2 — Сосуществование и смоук-тесты

- [ ] **4.2.1** [PROG-D] Проверить одновременную загрузку unity.so с другими модулями (renpy, rpgmaker, zanzarah, garbro) — отсутствие конфликтов CLR
- [ ] **4.2.2** [PROG-D] Смоук-тесты на реальных играх: Unity 5.x, Unity 2019–2021, Unity 2022+ — проверить что текстуры рендерятся, аудио воспроизводится

---

## Фаза 5: Пакетирование

Может начинаться параллельно с фазой 4.

- [ ] **5.1** [PROG-B] CPack-правила для модуля unity:
  - `unity-{DATE}-{ARCH}-dll.zip`: `unity.so`, `observer_user.ini`, `unity/AssetStudio.dll`, `unity/Texture2DDecoderNative.dll`, `licenses/`
  - PDB-пакет отдельно
  - x86 и x64 (AssetStudio.dll общая, нативные DLL платформо-зависимые)
- [ ] **5.2** [REVIEW-B] Ревью пакетирования
- [ ] **5.3** [PROG-B] Исправления по ревью

---

## Фаза 6: Финальное ревью

- [ ] **6.1** [REVIEW-ALL] Полное ревью всего кода: стиль, утечки памяти на границе managed/native, исключения, покрытие тестами
- [ ] **6.2** [PROG-ALL] Исправления
- [ ] **6.3** Полный прогон тестов (все модули включая unity), x86 и x64
- [ ] **6.4** Сборка релизных пакетов

---

## Карта параллелизма

```
Фаза 0.1 (интерфейс bridge.h)
    │
    ├───────────────────────┐
    ▼                       ▼
Фаза 0.2               Фаза 0.3
(скелет CMake)          (сборка AssetStudio
[PROG-B]                + ILRepack)
                        [PROG-B]
    │                       │
    ├───────────────────────┘
    │
    ├──────────────────┬──────────────────┐
    ▼                  ▼                  ▼
Фаза 1             Фаза 2            Фаза 3 (тесты)
(bridge impl)       (слой архива)     (dll.cpp тесты)
[PROG-A]            [PROG-B]          [PROG-C]
    │                  │                  │
    └──────────────────┴──────────────────┘
                       │
                       ▼
                 Фаза 3 (реализация)
                       │
                       ▼
                   Фаза 4
                 (интеграция)
                   [PROG-D]
                       │
                       ▼
                   Фаза 5
                 (пакетирование)
                       │
                       ▼
                   Фаза 6
                 (финальное ревью)
```

## Роли агентов

| Роль | Зона ответственности |
|------|---------------------|
| **PROG-A** | Bridge layer (C++/CLI) — `bridge.h`, `bridge.cpp`, тесты bridge |
| **PROG-B** | Система сборки + AssetStudio build + ILRepack + слой архива + мок + пакетирование |
| **PROG-C** | Точки входа DLL — `dll.cpp`, `unity.def`, `observer_user.ini`, тесты Observer API |
| **PROG-D** | Интеграционные тесты, смоук-тесты, тестовые данные |
| **REVIEW-*** | Ревью соответствующих PROG-агентов |
| **REVIEW-ALL** | Финальное сквозное ревью |

## Технические заметки

- `/clr` несовместим с `/EHsc` — для `bridge.cpp` использовать `/EHa`
- `/clr` несовместим со статическим CRT (`/MT`) — модуль unity целиком на `/MD`; остальные модули (renpy, rpgmaker, zanzarah) остаются на `/MT`
- `gcroot<T^>` — способ хранения managed-ссылок в нативных классах (внутри pimpl)
- Если модуль garbro тоже загружен — оба делят один CLR (оба под .NET Framework 4.7.2), конфликтов нет
- `AssetStudio.dll` — Any CPU, работает и в x86 и в x64 CLR
- `Texture2DDecoderNative.dll` — платформо-зависимая, должна соответствовать архитектуре unity.so
- **Резолв P/Invoke**: `unity::init()` должен вызвать `AddDllDirectory()` для подпапки `unity/`, чтобы P/Invoke из AssetStudio нашёл Texture2DDecoderNative.dll
- **Давление на память**: декодирование Texture2D может выделять большие RGBA-буферы (4096×4096 = 64 MB). Observer вызывает ExtractItem последовательно, так что это нормально.
- **AudioClip / FMOD**: часть аудио в Unity хранится в FSB5. AssetStudio (aelurum) включает поддержку FMOD — проверить что работает в net472 сборке. Если нужна нативная FMOD-библиотека, добавить в подпапку `unity/`.
- **Сжатие AssetBundle**: Unity использует LZMA (старые) и LZ4/LZ4HC (новые). AssetStudio обрабатывает оба прозрачно.
- **Type trees**: некоторые .assets файлы не содержат встроенных type trees. AssetStudio включает фоллбэк type trees для популярных версий Unity — убедиться что они попадают в ILRepack-merged сборку.
- Тестовые Unity-файлы < 1 MB, коммитятся в директорию тестовых данных.
- Обновление AssetStudio: `cd extern/AssetStudio && git pull && cd ../.. && git add extern/AssetStudio && git commit`
