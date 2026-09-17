#!/usr/bin/env bash
# Toggle header pin 32 (PD5, gpiochip0 line 101) slowly so the fan's response
# to each physical level can be watched. Physical levels only: no -l here.
#
# Stops fan-control for the duration and starts it again on exit, whatever the
# exit path. Both levels are plain 3.3 V logic and safe for the fan input.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: sudo bash deploy/fan-pin-test.sh'; exit 1; }

chip=gpiochip0
line=101
hold=${1:-6}
cycles=${2:-3}

was_active=0
systemctl is-active --quiet fan-control && was_active=1

finish() {
    pkill -f "gpioset -c $chip $line" 2>/dev/null
    if [ "$was_active" = 1 ]; then
        systemctl start fan-control
        echo "fan-control started again"
    fi
}
trap finish EXIT INT TERM

echo "=== pinmux PD5 ==="
if mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug 2>/dev/null; then
    grep -h 'PD5' /sys/kernel/debug/pinctrl/*/pinmux-pins 2>/dev/null | head -3 \
        || echo "no PD5 entry in pinmux-pins"
else
    echo "debugfs unavailable"
fi

if [ "$was_active" = 1 ]; then
    systemctl stop fan-control
    echo "fan-control stopped"
fi

echo
echo "=== released line (nobody drives pin 32) ==="
gpioinfo -c "$chip" "$line" 2>/dev/null
echo ">>> watch the fan: line released for ${hold}s"
sleep "$hold"

for i in $(seq "$cycles"); do
    for level in 0 1; do
        name=$([ "$level" = 0 ] && echo 'LOW  (0 V)' || echo 'HIGH (3.3 V)')
        echo
        echo ">>> cycle $i/$cycles: pin 32 = $name for ${hold}s — watch the fan"
        timeout "$hold" gpioset -c "$chip" "$line=$level" &
        sleep 0.5
        gpioinfo -c "$chip" "$line" 2>/dev/null | sed 's/^/    /'
        wait
    done
done

echo
echo "Done. Note which level stopped the fan:"
echo "  stopped on LOW  -> active high, fan-control.sh must call gpioset without -l"
echo "  stopped on HIGH -> active low, fan-control.sh needs -l"
echo "  never stopped   -> the level does not reach the fan, or it needs real PWM"
echo "Measured 17 September 2026: stops on LOW, runs on HIGH."
