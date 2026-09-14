# Передача разработки на Orange Pi

Дата: 2026-09-14, 10:58 UTC. Передача на Orange Pi 4 Pro завершена;
разработка временно остановлена по просьбе владельца. Документ предназначен
для любого разработчика и не требует доступа к переписке или конкретному инструменту.

## Что читать

1. [STATUS.md](../STATUS.md) — выполненные действия, даты проверок, ограничения.
2. [PLAN.md](../PLAN.md) — согласованные требования и целевая архитектура.
3. [README.md](../README.md) — установка и контракт API.
4. [NETWORK.md](NETWORK.md) и [ACCEPTANCE.md](ACCEPTANCE.md) — сеть и приёмка.

При расхождении план не считать доказательством выполненной настройки:
фактические результаты находятся в STATUS.md. Веб-интерфейс сейчас не разрабатывается.

## Где находятся файлы

| Место | Содержание |
| --- | --- |
| GitHub: dmitryweiner/monitoring | Репозиторий исходников; ветка main |
| Исходный компьютер: /home/dmw/projects/monitoring | Полная рабочая копия этого промежуточного результата |
| Orange Pi: /home/dmw/projects/monitoring | Git checkout, пользователь dmw, origin — тот же GitHub |
| Orange Pi: /opt/monitoring/monitoring | Установленные agent.py, common.py, spool.py, __init__.py |
| Orange Pi: /etc/monitoring/agent.toml | Конфигурация; адрес API пока пример, токен не установлен |
| Orange Pi: /var/lib/monitoring-agent/queue.db | Рабочая очередь; не заменять тестовой базой |
| Orange Pi: /etc/systemd/system/monitoring-agent.service | Служба агента |
| Orange Pi: /etc/systemd/system/monitoring-agent.service.d/collect-only.conf | Временный запуск без отправки |
| Orange Pi: /tmp/monitoring-check | Частичная копия кода для проверки, не Git checkout |
| Orange Pi: /tmp/monitoring-orange.tar.gz | Архив частичной копии; не резервная копия всего проекта |
| Orange Pi: /tmp/monitoring-apt.log | Журнал завершённой установки пакетов |

Содержимое /tmp может исчезнуть при перезагрузке; 14 сентября /tmp/monitoring-check
уже отсутствовал. Полный промежуточный результат передаётся через GitHub в рабочую
копию на Orange Pi. При отсутствии доступа использовать полную копию с исходного компьютера. Не использовать /opt/monitoring как каталог
разработки: работающий агент не должен получать изменения посреди выполнения.

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
14 сентября агент после включения платы работает в collect-only, NRestarts=0,
автозапуск включён. JPEG 46 488 байт, 1280×720 проверен ffprobe и полностью
декодирован ffmpeg без ошибок. Очередь при проверке не изменялась.
Tailscale показывает NeedsLogin. При продолжении проверить текущее состояние:

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

Tailscale установлен, tailscaled был active. Команда входа уже запускалась:
`sudo tailscale up --accept-dns=false --accept-routes=false --timeout=30s`.
Сначала проверить status; повторять вход только если устройство действительно
не авторизовано. Tailscale SSH, exit node и subnet routing не нужны.
Независимость от mihomo пока не настроена и не проверена.

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
2. Разобрать HTTP 403 из deploy/check-cloud.py на /healthz: последующий curl
   получил 200 с {"status":"ok"}. Причина различия не установлена.
   Команда сквозной проверки — в README. Тестовые загрузки ещё не выполнялись;
   временный белый JPEG — /tmp/monitoring-acceptance.jpg (32×24).
3. После успешной проверки API установить существующий secrets/device.token,
   настроить server_url, убрать только collect-only.conf, выполнить daemon-reload
   и restart агента. Проверить доставку/догрузку без дубликатов. Нужны права sudo;
   пароль не передавать через чат и не сохранять в проекте.
4. Авторизовать Tailscale, проверить обычный SSH. Изучить существующий mihomo,
   проверить обход TUN, DNS и DERP; изменения проводить с откатом.
5. Реализовать экспорт/восстановление D1/R2; выполнить приёмку и 48 часов наблюдения.
6. Подключить и испытать BMP280/DHT11, когда оборудование готово.

Старые monitoring/server.py, routing.py, серверные systemd units, backup.sh,
monitoring-route/tunnel и prepare-/activate-mihomo.sh относятся к прежнему VPS-плану.
Их нельзя применять как готовую настройку Cloudflare/Tailscale. Они сохранены
для переноса тестов и последующей уборки. deploy/install.sh использовать только
с аргументом agent после проверки необходимости повторной установки.

Не считать выполненными удалённый SSH при блокировке VPN, полный цикл облачной
доставки, восстановление резервной копии и 48-часовую приёмку — доказательств пока нет.
