# Передача разработки на Orange Pi

Дата: 2026-09-14, 11:12 UTC. Передача на Orange Pi 4 Pro завершена, работа
продолжается в рабочей копии на плате. Документ предназначен для любого
разработчика и не требует доступа к переписке или конкретному инструменту.

## Что читать

1. [STATUS.md](../STATUS.md) — выполненные действия, даты проверок, ограничения.
2. [PLAN.md](../PLAN.md) — согласованные требования и целевая архитектура.
3. [README.md](../README.md) — установка и контракт API.
4. [NETWORK.md](NETWORK.md) и [ACCEPTANCE.md](ACCEPTANCE.md) — сеть и приёмка.
5. [RUNBOOK-network.md](RUNBOOK-network.md) — пошаговый сетевой прогон,
   ближайшая незавершённая работа.

При расхождении план не считать доказательством выполненной настройки:
фактические результаты находятся в STATUS.md.

Веб-клиент ведётся отдельным репозиторием https://github.com/dmitryweiner/monitoring-client
и опубликован на https://dmitryweiner.github.io/monitoring-client/.
В этом репозитории он не лежит; единственная связь — ALLOWED_ORIGINS Worker.

## Где находятся файлы

| Место | Содержание |
| --- | --- |
| GitHub: dmitryweiner/monitoring | Репозиторий исходников; ветка main |
| GitHub: dmitryweiner/monitoring-client | Веб-клиент, отдельный проект; TypeScript, Vite, uPlot |
| Исходный компьютер: /home/dmw/projects/monitoring | Полная рабочая копия этого промежуточного результата |
| Orange Pi: /home/dmw/projects/monitoring | Git checkout, пользователь dmw, origin — тот же GitHub |
| Orange Pi: /opt/monitoring/monitoring | Установленные модули monitoring/*.py, включая motion.py и audio.py; обновлены 24 сентября |
| Orange Pi: /etc/monitoring/agent.toml | Рабочая конфигурация с адресом Worker, секциями [attention] и [audio]; рядом agent.toml.bak и agent.toml.bak-20260924 — копии до правок |
| Orange Pi: ~/monitoring-backup-20260924 | Резервная копия D1 и R2 перед миграцией 0002; частные фото, 0700 |
| Orange Pi: /etc/monitoring/device.token | Тот же токен, что в secrets/device.token; 0600 monitoring:monitoring |
| Orange Pi: /var/lib/monitoring-agent/queue.db | Рабочая очередь; не заменять тестовой базой |
| Orange Pi: /etc/systemd/system/monitoring-agent.service | Служба агента |
| Orange Pi: /etc/mihomo/config.yaml | Рабочая конфигурация VPN, 0600 root; боевые ключи |
| Orange Pi: /var/log/monitoring-net-test.log | Отчёты сетевых испытаний |
| Orange Pi: /etc/mihomo/config.yaml.bak-20260917-020405 | Конфигурация mihomo до замены другой сессией 17 сентября |
| Orange Pi: /root/mihomo-backup/ | Копии конфигурации mihomo до правки fake-ip-filter |
| Orange Pi: /etc/systemd/timesyncd.conf.d/ntp-by-ip.conf | NTP по IP-адресам; fake-hwclock-load замаскирован |
| Orange Pi: /usr/local/sbin/fan-control.sh | Управление вентилятором по температуре |
| Orange Pi: /etc/udev/rules.d/99-monitoring-gpio.rules | Доступ группы gpio к gpiochip0 для DHT11 |

Прежние /tmp/monitoring-check, /tmp/monitoring-orange.tar.gz и /tmp/monitoring-apt.log
к 14 сентября исчезли: содержимое /tmp не переживает перезагрузку. Полный
промежуточный результат передаётся через GitHub в рабочую копию на Orange Pi.
При отсутствии доступа использовать полную копию с исходного компьютера.
Не использовать /opt/monitoring как каталог разработки: работающий агент
не должен получать изменения посреди выполнения.

Копию конфигурации mihomo держать вне Git: config.yaml, mihomo/ и cache.db
внесены в .gitignore, но надёжнее класть такую копию в secrets/.

На самой плате под пользователем dmw:

```sh
cd ~/projects/monitoring
git status --short
git pull --ff-only origin main
git log -3 --oneline
```

Сначала сохранить местные изменения, если git status не пуст. Последний промежуточный коммит должен содержать этот HANDOFF.md.

## Завершённая проверка и следующий шаг

Плата: 192.168.100.196, пользователь dmw. Пароль и другие секреты в документы
не записываются. Старая BPI 192.168.100.135 больше не используется.
14 сентября в 11:15 UTC агент переведён в штатный режим: собирает и отправляет,
NRestarts=0, автозапуск включён. JPEG 46 488 байт, 1280×720 проверен ffprobe
и полностью декодирован ffmpeg без ошибок. Съёмка отказывала с 03:21 до 09:17 UTC
и с тех пор работает. Tailscale не авторизован, состояние Logged out.
При продолжении проверить текущее состояние:

```sh
sudo systemctl --no-pager status monitoring-agent tailscaled
sudo systemctl --no-pager show monitoring-agent -p ActiveState -p SubState -p NRestarts
sudo journalctl --no-pager -u monitoring-agent -n 50
sudo tailscale status
df -h /
timedatectl show -p NTPSynchronized
```

Использовать --no-pager: ранее pager поглотил символ следующей команды
и проверочная команда извлечения JPEG не выполнилась. Не запускать второго агента
на рабочей очереди. Для проверки картинки прочитать копию одного JPEG из SQLite
в режиме read-only, проверить ffprobe и полное декодирование ffmpeg; после проверки
убрать временное фото. Не удалять события из очереди ради этой проверки.

Tailscale установлен, tailscaled active, но устройство не авторизовано.
Вход, четыре режима испытаний и место для записи результатов —
[RUNBOOK-network.md](RUNBOOK-network.md); обоснование и разбор fake-ip —
[NETWORK.md](NETWORK.md). Сначала проверить status; Tailscale SSH, exit node
и subnet routing не нужны. Независимость от mihomo не настроена и не проверена:
сеть на плате пока только читали.

## Среда разработки

- Агент: Python 3.11+, на плате установлен 3.13.5; ffmpeg и v4l-utils установлены.
- Облачный backend: TypeScript, Wrangler 4.131.2, Miniflare 5.20260911.1-alpha.
  Версии зависимостей зафиксированы cloud/package-lock.json.
- Установлен ARM64 Node 24.21.0 (проверено 14 сентября). Wrangler/Miniflare требуют
  Node 22+; на исходном компьютере portable-тесты проходили с Node 22.22.1.
  ARM64 workerd проверен на плате: 9 тестов Miniflare с D1/R2 прошли 14 сентября.
- Не копировать node_modules или Python .venv с x86-компьютера на ARM64.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
cd cloud
npm ci
npm run check
npm test
```

Если workerd недоступен, npm run test:portable проверяет логику через Node/SQLite
с тестовым адаптером. Такой результат не подтверждает настоящие D1/R2 bindings,
лимиты Cloudflare или независимость SSH. Исторические результаты: 17 Python-тестов
и 8 portable-тестов прошли 11 сентября; Miniflare тогда не стартовал на Atom N450.
Тестовый endpoint /__test_cleanup находится только в cloud/test/entry.ts;
публиковать следует cloud/src/index.ts через конфигурацию Wrangler.

## Облако и доступы

D1 уже создана, миграция применена; UUID и account_id находятся в
cloud/wrangler.jsonc и STATUS.md. Не создавать вторую одноимённую базу.
R2 активирован владельцем; закрытый bucket home-monitoring-poc-photos создан.
Worker опубликован: https://home-monitoring-poc.dmitry-weiner.workers.dev.
workers.dev уже настроен. Доставка агента пока не включена.

Wrangler авторизован на Orange Pi владельцем 14 сентября; вход проверен. Не помещать OAuth, GitHub-токены,
пароль SSH, device.token, admin.key или VPN-конфигурацию в Git.
Секреты приложения созданы в secrets/ с правами 0600: device.token, admin.key,
server.env и worker.json (хеши с именами Worker). DEVICE_HASH/ADMIN_HASH уже
загружены в Worker через stdin; в /etc/monitoring токен ещё не установлен.
Не генерировать их повторно. secrets/ исключён из Git.

На Orange Pi уже работает mihomo v1.19.30 ARM64 с конфигурацией /etc/mihomo,
автозапуск включён (проверено 14 сентября). Эта установка появилась после прежнего
этапа; её параметры обхода TUN и DNS не проверялись. Не заменять работающую
конфигурацию автоматически. Сначала изучить её и сохранить защищённую копию.

Исходный комплект платного VPN остаётся на прежнем компьютере в /home/dmw/mihomo:
config.yaml, install-mihomo.sh, cache.db. Он не входит в Git. Старый установщик
сразу включает TUN и не обеспечивает проверенный откат; не запускать его повторно
поверх существующей настройки.

## Порядок продолжения

Сначала прочитать точку остановки в STATUS.md. После остановки владелец запросил
коммит с текущим результатом. Перед git pull проверить git status и сохранить
возможные новые местные изменения.

1. Среда и локальные тесты готовы: Python 17/17, Miniflare 9/9, portable 9/9,
   TypeScript прошёл; OpenAPI дополнен схемами. Повторять при изменении кода.
2. HTTP 403 разобран: Cloudflare отклоняет User-Agent `Python-urllib/*`
   на границе (`error code: 1010`). Явный заголовок добавлен в monitoring/agent.py
   и deploy/check-cloud.py; без него отправка с платы тоже получала бы 403.
   Сквозная проверка настоящего Worker пройдена, команда — в README.
3. Доставка с платы включена 14 сентября в 11:15 UTC: код обновлён, токен
   установлен, server_url прописан, collect-only.conf снят. Накопленная очередь
   ушла в облако без дубликатов. Остаётся наблюдение за расходом суточных лимитов.
   Системные изменения по-прежнему требуют sudo: dmw запрашивает пароль,
   вводит его владелец в своём терминале. Пароль не передавать через чат.
4. Сетевой прогон: пошаговый порядок в docs/RUNBOOK-network.md, обоснование
   и разбор fake-ip — в docs/NETWORK.md. Скрипты deploy/net-inspect.sh
   и deploy/net-failover-test.sh готовы и ни разу не запускались.
5. Экспорт/восстановление D1/R2 готовы: deploy/backup-cloud.py и
   deploy/restore-cloud.py, восстановление сверено в отдельных тестовых ресурсах
   14 сентября. Остаются сроки хранения 30/90 дней, приёмка и 48 часов наблюдения.
   Тестовые home-monitoring-restore-test и -photos владелец решил оставить
   как площадку для проверки сроков хранения; удалить после неё.
6. Подключить и испытать BMP280/DHT11, когда оборудование готово.

Старые monitoring/server.py, routing.py, серверные systemd units, backup.sh,
monitoring-route/tunnel и prepare-/activate-mihomo.sh относятся к прежнему VPS-плану.
Их нельзя применять как готовую настройку Cloudflare/Tailscale. Они сохранены
для переноса тестов и последующей уборки. deploy/install.sh использовать только
с аргументом agent после проверки необходимости повторной установки.

Не считать выполненными удалённый SSH при блокировке VPN и 48-часовую приёмку —
доказательств пока нет. Восстановление копии проверено сверкой с файлами копии;
работа Worker поверх восстановленных ресурсов не испытывалась.
Доставка с платы подтверждена на настоящих данных 14 сентября,
но длительный прогон под суточными лимитами ещё не выполнен.
