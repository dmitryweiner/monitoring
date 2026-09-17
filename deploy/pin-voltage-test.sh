#!/usr/bin/env bash
# Hold free header pins high for a while so their voltage can be measured, and
# show that the kernel really holds them as outputs while the meter is on them.
#
# Pins used: 3 (PB3, line 35) and 29 (PD0, line 96), both free. For a known-good
# reference the fan-control service is paused and pin 32 (PD5, line 101) is held
# high too — the fan spinning proves that output is real. The service is started
# again on exit, whatever the exit path.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: sudo bash deploy/pin-voltage-test.sh'; exit 1; }

hold=${1:-120}
chip=gpiochip0
lines=(35 96 101)

fan_was_active=0
systemctl is-active --quiet fan-control && fan_was_active=1
holder=''
finish() {
    [ -n "$holder" ] && kill "$holder" 2>/dev/null
    wait 2>/dev/null
    if [ "$fan_was_active" = 1 ]; then
        systemctl start fan-control && echo "fan-control started again"
    fi
}
trap finish EXIT INT TERM

[ "$fan_was_active" = 1 ] && systemctl stop fan-control

gpioset -c "$chip" 35=1 96=1 101=1 &
holder=$!
sleep 1
if ! kill -0 "$holder" 2>/dev/null; then
    echo "gpioset exited at once: the lines could not be requested"
    exit 1
fi

echo "=== what the kernel reports for the held lines ==="
gpioinfo -c "$chip" "${lines[@]}"
echo
cat <<EOF
Held high for ${hold}s. The fan should be spinning now: that proves pin 32 is driven.
Measure against GND (pin 9, 30 or 34):

  pin 32 (PD5, fan)   -> reference: a pin known to be driven high
  pin 29 (PD0)        -> same bank as the DHT11 and the fan
  pin 3  (PB3)        -> the bank the BMP280 would use
  pin 1               -> the 3.3 V rail

EOF
for ((left = hold; left > 0; left -= 10)); do
    if ! kill -0 "$holder" 2>/dev/null; then
        echo "gpioset stopped early; values below are no longer held"
        break
    fi
    printf 'still holding, %3ds left\n' "$left"
    sleep $((left < 10 ? left : 10))
done
