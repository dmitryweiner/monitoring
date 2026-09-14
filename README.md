# Домашний мониторинг

Python-агент на SBC собирает температуру CPU и JPEG каждые 10 минут. Неотправленное
хранится в SQLite до суток. Cloudflare Worker принимает данные по HTTPS: D1 хранит
показания 90 дней, закрытый R2 — фото 30 дней. SSH — штатный OpenSSH через Tailscale.
Веб-интерфейс не входит в этот этап. Требования — [PLAN.md](PLAN.md),
фактическое состояние — [STATUS.md](STATUS.md).

Разработка приостановлена 2026-09-14 для продолжения на Orange Pi.
Инструкция передачи, карта файлов и оставшиеся задачи — [docs/HANDOFF.md](docs/HANDOFF.md).

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

Войти через `wrangler login`. В панели включить R2 и открыть Workers & Pages
для создания workers.dev. Активация R2 и платёжных условий выполняется владельцем
аккаунта; платный тариф Workers не назначается автоматически.

Из cloud/, предварительно проверив отсутствие одноимённых ресурсов:

```sh
wrangler d1 create home-monitoring-poc
wrangler r2 bucket create home-monitoring-poc-photos
```

В wrangler.jsonc записать полученный database_id. Bucket оставить закрытым,
не включать публичный r2.dev. Затем применить миграции:

```sh
wrangler d1 migrations apply home-monitoring-poc --remote
```

Из корня проекта выполнить
`python3 -m monitoring.admin credentials secrets --device-id home`.
Команда создаёт device.token, admin.key и server.env с правами 0600,
не печатает ключи и не заменяет существующие. server.env пока использует имена
старого backend: значение MONITOR_DEVICE_HASH загрузить как secret DEVICE_HASH,
MONITOR_ADMIN_HASH — как ADMIN_HASH. Использовать stdin Wrangler, не передавать
секреты аргументами команд или в URL. На плату нужен только device.token.
После загрузки secrets выполнить `wrangler deploy` и проверить API.

Развёртывание не завершено, пока его результаты не зафиксированы в STATUS.md.

## SBC

Агенту нужны Python 3.11+ и ffmpeg, сторонние Python-пакеты не требуются.
Текущая плата — Orange Pi 4 Pro, 192.168.100.196: Armbian/Debian 13,
Python 3.13, ARM64. Старая BPI M1+ больше не используется.

```sh
sudo bash deploy/install.sh agent
sudo install -m 0600 -o monitoring -g monitoring /path/to/device.token /etc/monitoring/device.token
sudoedit /etc/monitoring/agent.toml
```

Указать HTTPS-адрес Worker. Камера использует постоянный путь
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

До готовности облака установлен drop-in
/etc/systemd/system/monitoring-agent.service.d/collect-only.conf с --collect-only.
После настройки адреса и токена удалить именно этот drop-in, выполнить
`systemctl daemon-reload` и `systemctl restart monitoring-agent`.
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
ALLOWED_ORIGINS задаётся при интеграции UI. Для разных сайтов использовать Bearer
в памяти клиента либо прокси API под доменом UI. Изменение ADMIN_HASH
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

## Доступ и приёмка

Tailscale и обход mihomo — [docs/NETWORK.md](docs/NETWORK.md).
Экспорт D1/R2 и проверка восстановления ещё предстоят; прежний VPS backup
не является резервной копией Cloudflare. Критерии — [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).
BMP280/DHT11 ждут подключения; будущие sysfs/IIO каналы добавляются через sources.
Сетевые отказы, физические датчики и 48-часовой прогон проверяются отдельно.
