# Домашний мониторинг

Python-агент на SBC собирает температуру CPU и JPEG каждые 10 минут. Неотправленное
хранится в SQLite до суток. Cloudflare Worker принимает данные по HTTPS: D1 хранит
показания 90 дней, закрытый R2 — фото 30 дней. SSH — штатный OpenSSH через Tailscale.
Веб-клиент разрабатывается отдельным проектом и в этот репозиторий не входит:
исходники https://github.com/dmitryweiner/monitoring-client,
опубликован на https://dmitryweiner.github.io/monitoring-client/.
Здесь он влияет только на ALLOWED_ORIGINS. Требования — [PLAN.md](PLAN.md),
фактическое состояние — [STATUS.md](STATUS.md).

Инструкция передачи, карта файлов и оставшиеся задачи — [docs/HANDOFF.md](docs/HANDOFF.md).
Ближайшая незавершённая работа — сетевой прогон по
[docs/RUNBOOK-network.md](docs/RUNBOOK-network.md).

Клиенты обязаны посылать собственный заголовок User-Agent: Cloudflare отклоняет
строку `Python-urllib/*` на границе с `error code: 1010` ещё до запуска Worker.

## Проверка

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
cd cloud
npm ci
npm run check
npm test
```

Для cloud-инструментов требуется Node 22+; на Orange Pi уже установлен Node 24.21.0
(проверено 14 сентября). workerd на x86 требует SSE4.2 и CLMUL;
на прежнем компьютере разработки Atom N450 их нет.
`npm run test:portable` проверяет тот же Worker через Node 22.13+ и SQLite
с тестовым адаптером D1/R2. Это не заменяет испытания bindings, квот и CPU в
Miniflare или Cloudflare. Python-тесты пока используют старый FastAPI backend
как эталон протокола. Серверные службы VPS и скрипты обратного SSH из deploy
не входят в развёртывание Cloudflare/Tailscale.

## Cloudflare

Worker опубликован: https://home-monitoring-poc.dmitry-weiner.workers.dev.
D1 home-monitoring-poc уже существует, миграции применены. R2 активирован владельцем,
bucket home-monitoring-poc-photos создан в классе Standard без публичного доступа.
Wrangler на Orange Pi авторизован. Повторно создавать ресурсы не нужно.

Ключи созданы в secrets/ с правами 0600, каталог исключён из Git:
device.token, admin.key, server.env и worker.json. Последний содержит только хеши
DEVICE_HASH/ADMIN_HASH; они уже загружены в Worker через stdin. Не генерировать
новые ключи при продолжении. Тот же device.token установлен в /etc/monitoring
на плате; агент с 14 сентября 11:15 UTC работает без --collect-only.

TypeScript, 17 Python-тестов, 9 Miniflare и 9 portable-тестов прошли на ARM64.
Сквозная проверка настоящего Worker пройдена 14 сентября: health, авторизация,
загрузка измерения и JPEG, повтор без дубликатов, закрытое чтение, история, logout.
Повторить её из корня можно так:

```sh
python3 deploy/check-cloud.py \
  --url https://home-monitoring-poc.dmitry-weiner.workers.dev \
  --device-token secrets/device.token --admin-key secrets/admin.key \
  --photo /path/to/non-sensitive.jpg
```

Скрипт при успешном запуске оставляет тестовое измерение и JPEG с source=acceptance.
Ключи читает из файлов и не печатает. Подходит любой небольшой JPEG; белый снимок
32×24 создаётся командой
`ffmpeg -f lavfi -i color=c=white:s=32x24 -frames:v 1 out.jpg`.
Подробные результаты и следующий шаг — в STATUS.md.

## SBC

Агенту нужны Python 3.11+ и ffmpeg, сторонние Python-пакеты не требуются.
Текущая плата — Orange Pi 4 Pro, 192.168.100.196: Armbian/Debian 13,
Python 3.13, ARM64. Старая BPI M1+ больше не используется.

```sh
sudo bash deploy/install.sh agent
sudo install -m 0600 -o monitoring -g monitoring /path/to/device.token /etc/monitoring/device.token
sudoedit /etc/monitoring/agent.toml
```

Указать HTTPS-адрес Worker. Датчик DHT11 описывается источником с
`type = "dht11"` (пример в deploy/agent.toml.example) и отдаёт `humidity_percent`
и `temperature_c`. Ему нужен доступ к `/dev/gpiochip0`: группа `gpio`, правило udev
и `SupplementaryGroups=video gpio` в службе — всё это ставит
`sudo bash deploy/enable-dht11.sh`. Подробности — [docs/PINOUT.md](docs/PINOUT.md).

Камера использует постоянный путь
/dev/v4l/by-id/usb-Web_Camera_Web_Camera_202512181-video-index0, MJPEG 1280×720.
ffmpeg пропускает первые 0,5 секунды из-за пустых начальных кадров.
Нулевое дополнение после конца JPEG удаляется; обрезанный снимок не принимается.

Для разовой проверки при остановленной службе:

```sh
cd /opt/monitoring
sudo -u monitoring python3 -m monitoring.agent --once --collect-only
sudo systemctl enable --now monitoring-agent
sudo journalctl -u monitoring-agent --since '1 hour ago'
```

Drop-in collect-only.conf снят 14 сентября: агент собирает и отправляет.
Если отправку нужно снова приостановить, вернуть тот же drop-in с `--collect-only`,
выполнить `systemctl daemon-reload` и `systemctl restart monitoring-agent`.
Не запускать несколько агентов на одной очереди.

Очередь ограничена сутками, 512 MiB полезных данных и резервом 1 GiB диска.
Старейшие записи удаляются при достижении ограничений, dropped учитывает потери.
Служебные страницы SQLite и журнал требуют дополнительного места.

## API v1

Машиночитаемое описание: [cloud/openapi.json](cloud/openapi.json).

Время — Unix seconds UTC. observed_at обозначает время измерения,
received_at — приём облаком. Температура CPU имеет поле cpu_temperature_c.

| Метод и путь | Назначение | Авторизация |
| --- | --- | --- |
| GET /healthz | Worker и D1 | Нет |
| POST /v1/measurements | До 12 событий | Device Bearer |
| POST /v1/photos | JPEG до 4 MiB, JSON в X-Event | Device Bearer |
| POST /v1/session | JSON с key, получение сессии | Admin key |
| DELETE /v1/session | Отзыв текущей сессии | Session |
| GET /v1/latest | Последние значения и last_seen | Session |
| GET /v1/measurements | История: start/end/source/limit/cursor | Session |
| GET /v1/measurements/aggregate | source/metric/start/end/bucket_seconds | Session |
| GET /v1/photos | История фотографий | Session |
| GET /v1/photos/{event_id} | Закрытый JPEG | Session |

Сессия действует 7 дней, передаётся через Bearer или Secure/HttpOnly cookie.
Для DELETE с cookie нужны разрешённый Origin и X-CSRF-Token: 1.
ALLOWED_ORIGINS содержит https://dmitryweiner.github.io — источник веб-клиента.
Это только схема и хост: путь /monitoring-client/ в источник не входит.
localhost намеренно отсутствует: dev-сервер клиента проксирует запросы к Worker
и снимает заголовок Origin. Для других сайтов использовать Bearer в памяти
клиента либо прокси API под доменом UI. Изменение ADMIN_HASH
инвалидирует существующие сессии.

Событие содержит schema_version=1, device_id, UUID event_id, observed_at,
kind=measurement|photo, source, status=ok|error, values и clock_synchronized.
Повтор получает duplicate, конфликт — conflict. Агент удаляет только явно
подтверждённые stored/duplicate/expired. Фото резервируется в D1, записывается
в R2 и переводится в ready до ACK; повтор завершает прерванную операцию.
Очистка выполняется каждые 15 минут.

Ограничения PoC: 6 GB фото, 300 новых фото и 4096 измерений за сутки UTC.
Они уменьшают риск превышения бесплатных квот, но не являются платёжным лимитом
Cloudflare. Расход запросов и CPU требует облачного прогона.

## Резервная копия облака

Прежний VPS backup не является резервной копией Cloudflare. Для D1 и закрытого R2
используются два скрипта; обоим нужен авторизованный Wrangler, сторонних пакетов нет.

```sh
python3 deploy/backup-cloud.py --out ~/monitoring-backup-$(date -u +%Y%m%d)
python3 deploy/restore-cloud.py --backup ~/monitoring-backup-YYYYMMDD   --database home-monitoring-restore-test   --bucket home-monitoring-restore-test-photos --create
```

Копия содержит частные фотографии и хеши сессий: каталог создаётся с правами 0700,
файлы 0600. Хранить защищённо и удалять старые копии по местной политике.

Восстанавливать только в отдельные ресурсы: скрипт отказывается писать в те,
из которых снята копия, и требует пустую базу. Миграцию к ней применять НЕ нужно —
схема уже внутри дампа. Порядок в дампе важен: данные идут до создания триггеров,
поэтому при загрузке не срабатывают check_quota и event_usage. Применение миграции
заранее сломало бы восстановление: сработал бы суточный лимит фотографий,
а usage.photo_bytes удвоился бы.

После загрузки restore-cloud.py сверяет результат с файлами копии, а не с рабочей
базой: таблицы D1 построчно и объекты R2 побайтово. Ключи объектов берутся из D1,
поэтому осиротевший объект R2 без строки в D1 в копию не попадёт; Worker такой
объект тоже не отдаёт. Автоматическое внешнее резервирование — следующий этап.

## Доступ и приёмка

Tailscale и обход mihomo — [docs/NETWORK.md](docs/NETWORK.md).
Критерии приёмки — [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).
BMP280/DHT11 ждут подключения; будущие sysfs/IIO каналы добавляются через sources.
Сетевые отказы, физические датчики и 48-часовой прогон проверяются отдельно.
