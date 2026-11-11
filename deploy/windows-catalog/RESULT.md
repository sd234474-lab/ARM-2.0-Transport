# Результат ARM-WIN-STRUCT-001

На Windows добавлен только новый пользовательский каталог `C:\ARM\00_START` и новый desktop shortcut **ARM - Catalog**. Старые файлы, базы, working paths, launchers/shortcuts и настройки запуска сохранены. Никакой физический перенос или запуск АРМ не выполнялся. Docker и ARM2 не устанавливались. Уточнение владельца учтено: все обнаруженные базы АРМ — прототипы, рабочих баз нет.

## Карта реальности

| Вариант | Подтверждённая версия / конфигурация | База / запуск |
|---|---|---|
| Старый ARM-MVP | commit `13b1a678…`; 12 SQL migrations | 3 SQLite схемы 12, ручной выбор старым launcher, 8081; отдельно нулевой файл |
| Промежуточный TEST source | `948bf342…`; 16 migrations | Staging сценарий указывает отсутствующую DB и порт 8082; содержит fetch/pip/migrate, не является безопасной кнопкой открыть |
| Установленный TEST Launcher | `08086390…`; 22 migrations; 2 дополнительных untracked helper `.py`; установленный create GUI совпал с overlay V3 по SHA256 | 2 registered TEST DB схемы 22; выбор в GUI/registry; runtime Python 3.13, web 8080. `ACTIVE` в registry не означает рабочую/production DB |
| Локальный contractor | `a3f0c6fa…`; code migrations до 0055; PostgreSQL runtime 17 | Отдельная local test DB, web 5174/API 8014/PG 55432; PG не слушает, фактическая DB schema НЕ УСТАНОВЛЕНО; не запускался ради запроса |
| AutotuningWorkplace | exe FileVersion 16.0.0; FileDescription «Настройка рабочего места ЕИС» | Сторонняя программа, не ARM-MVP; конфигурация/DB/URL НЕ УСТАНОВЛЕНО; не запускалась |
| Два rollback source | `fb4d34ae…` (11 migrations), `756c79a7…` (12) | Разные source fingerprints; исторические DB, restore не принимался |
| ARM2 UAT | source `1f8c06d…`, подготовленный package PR #38 | **НЕ УСТАНОВЛЕН**, запущенной DB/экземпляра нет; не смешивать с локальным contractor |

Локальная модель: 8 платформ/версий (включая historical/planned), 8 конфигураций, 7 вариантов запуска. В 14 строках данных отдельно показаны 9 действительных SQLite DB (из них 4 исторические), 3 нулевых/incomplete артефакта, недоступная PG DB и отсутствующая planned staging DB. Launcher registry и 2 technical PROJ SQLite **не** выданы за предметные ИБ АРМ.

Рекурсивная metadata-инвентаризация охватила найденные ARM/ARM-MVP/Autotuning roots и launchers. В одном technical pytest cache доступ не установлен; агрегированный размер его предков — нижняя оценка. Credentials/предметные строки не читались/не публиковались. Полный локальный реестр, actual paths, schema metadata, sizes/dates и hashes находятся на Windows, не в Git.

Все пять исследованных Python/SQL source-наборов имеют разные fingerprints. Найдены отдельные побайтные совпадения DB текущего снимка с историческими копиями; это не основание удалять копии с разным происхождением/ролью. Исторический backup/rollback не объявлен пригодным по одному размеру или checksum.

## Что организовано и как пользоваться

Открыть **ARM - Catalog** на рабочем столе или `C:\ARM\00_START\index.html`. Поиск работает по коду, названию, версии и пути. Разделы отвечают на вопросы «что исполняется», «какие правила/схема», «где данные», «какой launcher выбирает базу», «откуда release/backup». Кнопки копируют путь, а не запускают приложение.

В `01_PLATFORMS`–`05_RELEASES_BACKUPS` — 14 новых навигационных `.lnk` только на существующие каталоги. Нет нового неоднозначного app launcher/default DB. Существующие ярлыки/Центр запуска продолжают использоваться по-прежнему. Явно опасный установочный staging-сценарий не представлен как обычный запуск.

Отдельный **PHYSICAL-CHANGE-PLAN.md** содержит 13 поштучных предложений с from/to/users/preconditions/verify/rollback и статусом «НЕ СОГЛАСОВАНО», включая три старые DB по отдельности. Переносы не выполнены. Необходимые следующие решения: backup+restore verification, выбор приоритетной базы прототипа при необходимости, retention/provenance исторических наборов и только затем согласование конкретного физического изменения по ID.

**RELEASES-AND-ARTIFACTS.md** отдельно перечисляет 7 найденных INPUT-наборов с размером/числом файлов/комплектацией и границей доказанности применения. Пакет V3 связан с установленным helper через SHA256; остальные наборы не объявлены мусором по имени. Эти локальные документы лежат рядом с `index.html`, их можно открыть из Проводника.

## Доказательства

- 81 существующий файл (SQLite/registry/launchers/старые desktop links и exe) совпал до/после по SHA256, size и mtime; 203 Python/SQL файла пяти старых source roots также неизменны по manifest fingerprints. Это наблюдение снимка, не гарантия против будущих внешних изменений.
- Все 14 folder shortcuts и новый desktop target проверены через COM **без запуска**. Старые shortcuts не сохранялись/не переписывались. New target существовал только после установки нового каталога; повторный install откажется от перезаписи.
- Headless browser проверил установленный local HTML: 59 строк, 6 разделов, поиск, desktop/narrow viewport, 0 page errors, 0 HTTP requests. Это проверка нового каталога, не GUI-приёмка АРМ.
- 9 unittest проверок ro metadata/sidecars/privacy/schema links/escaping/overwrite guards/change detection/PowerShell parsing — PASS. Ruff и JS syntax — PASS.
- Полные before/after/shortcut evidence — `C:\ARM\00_START\90_EVIDENCE`. В публичный PR не отправлены пользовательские registry/DB/конфиги/логи/credential values или персональные пути desktop.

При первой попытке добавление корректно остановилось **до записи** из-за скрытого старого `.lnk`; read-only проверка исправлена (`Get-Item -Force`), старый файл/атрибуты не менялись. Успешная повторная попытка создала только новые элементы. Нет merge/deploy, миграций, переноса данных, ожидания GitHub CI или установки ARM2.
