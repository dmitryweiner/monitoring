# SSH через Tailscale и независимость от mihomo

Используется обычный OpenSSH внутри Tailscale. Публичный IP дома,
обратный SSH на VPS и Cloudflare Tunnel для этого не требуются.
Фактические этапы записываются в [STATUS.md](../STATUS.md).

## Установка и вход

На Debian 13 используется [официальный репозиторий](https://pkgs.tailscale.com/stable/)
Tailscale для trixie, пакет arm64 для текущей Orange Pi 4 Pro.

```sh
sudo systemctl enable --now tailscaled
sudo tailscale up --accept-dns=false --accept-routes=false
```

Авторизовать плату и компьютер администратора в одном tailnet.
Не включать exit node, subnet routing и Tailscale SSH: порт 22 обслуживает sshd.
Проверить tailscale status, tailscale netcheck, tailscale ip -4,
затем открыть новую сессию ssh dmw@<tailscale-ip>.
Fingerprint сверить через доверенное LAN-соединение.
Проверить срок действия ключа платы для долгой автономной работы.

## Обход TUN

Настройка ещё требует испытания; прежние monitoring-route, monitoring-tunnel,
prepare-mihomo.py и activate-mihomo.sh рассчитаны на VPS и не применяются здесь.

Tailscale использует policy rules, отдельную таблицу маршрутизации и метки
исходящего транспорта. До активации mihomo проверить ip -4 rule, ip -6 rule,
маршруты установленной версии и правила nftables. Tailscale должен обрабатывать
свой транспорт и tailnet до перехвата TUN. Исключение всего UID root
не заменяет эту проверку.

Отдельно проверить DNS для control plane и DERP при неработающем VPN:
--accept-dns=false само по себе не защищает DNS от mihomo fake-ip/dns-hijack.
Нужен проверенный прямой DNS управляющего канала или соответствующее исключение.
Адреса DERP меняются; одного IP-исключения недостаточно.

Платный VPN переносится из ~/mihomo с сохранением оригинала.
Orange Pi требует бинарник ARM64 и проверку через mihomo -t.
Первую активацию выполнять с таймером отката и сохранённой LAN-сессией,
после успешной проверки Tailscale без mihomo.

## Испытание

Для каждого состояния открыть новую SSH-сессию из внешней сети:
без mihomo; с TUN; с недоступным VPN; после перезапуска mihomo;
после загрузки SBC с недоступным VPN. Проверить прямой Tailscale UDP,
DERP, DNS, смену DHCP и реальный модем.

Сбой VPN задерживает доставку: для этого есть суточная очередь.
Независимость SSH считается достигнутой только после испытаний.
Блокировка самого Tailscale и потеря интернета остаются отдельными отказами.
