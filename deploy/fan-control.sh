#!/usr/bin/env bash
# Switches the case fan on and off by CPU utilisation. On/off only, no PWM.
#
# The fan is a Raspberry Pi Active Cooler on header pin 32 (PD5). Its PWM input
# is active low, so the line is pulled down to spin the fan and driven high to
# stop it; see docs/PINOUT.md for the wiring and for why that is safe to drive
# straight from a 3.3 V pin.
#
# gpioset owns a line only while it runs, so the value is held by a background
# gpioset that is replaced whenever the state changes. Releasing the line lets
# the fan return to full speed, which is the failure this should have.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: it opens /dev/gpiochip0'; exit 1; }
command -v gpioset >/dev/null || { echo 'gpioset is missing: apt install gpiod'; exit 1; }

chip=${FAN_CHIP:-gpiochip0}
line=${FAN_LINE:-101}           # PD5: bank D is the fourth, so 3 * 32 + 5

load_on=${FAN_LOAD_ON:-60}      # start above this utilisation, percent of all cores
load_off=${FAN_LOAD_OFF:-30}    # stop below it; the gap is the hysteresis
min_on=${FAN_MIN_ON:-60}        # once started, keep running this long, seconds
poll=${FAN_POLL:-5}             # sampling window, seconds

# Independent of load: run the fan above this temperature however idle the board
# looks, because utilisation lags heat and a hot passive board reads as idle.
# Set to 0 to drop the safety net and switch purely on load.
temp_force=${FAN_TEMP_FORCE:-70000}   # millidegrees, as in /sys/class/thermal/*/temp
zones=${FAN_ZONES:-'cpub_thermal_zone cpul_thermal_zone gpu_thermal_zone'}

temp_files=()
for zone in /sys/class/thermal/thermal_zone*; do
    type=$(cat "$zone/type" 2>/dev/null) || continue
    case " $zones " in *" $type "*) temp_files+=("$zone/temp");; esac
done
if [ "$temp_force" -gt 0 ] && [ ${#temp_files[@]} -eq 0 ]; then
    echo "none of the thermal zones exist: $zones" >&2
    exit 1
fi

# Utilisation between two calls. Keeps its counters in globals on purpose: a
# command substitution would run this in a subshell and lose them every tick.
prev_total=0
prev_idle=0
busy=0
sample_cpu() {
    local _name user nice system idle iowait irq softirq steal total quiet delta_total delta_idle
    read -r _name user nice system idle iowait irq softirq steal _ < /proc/stat
    total=$((user + nice + system + idle + iowait + irq + softirq + steal))
    quiet=$((idle + iowait))
    delta_total=$((total - prev_total))
    delta_idle=$((quiet - prev_idle))
    prev_total=$total
    prev_idle=$quiet
    if [ "$delta_total" -le 0 ]; then busy=0; else busy=$(((delta_total - delta_idle) * 100 / delta_total)); fi
}

hottest() {
    local max=0 value
    for file in "${temp_files[@]}"; do
        value=$(cat "$file" 2>/dev/null) || continue
        [ "$value" -gt "$max" ] && max=$value
    done
    echo "$max"
}

gpio_pid=''
gpio_state=''
set_fan() {   # 1 = spinning, 0 = stopped; -l maps that onto the active-low input
    local want=$1
    [ "$gpio_state" = "$want" ] && return
    if [ -n "$gpio_pid" ]; then
        kill "$gpio_pid" 2>/dev/null
        wait "$gpio_pid" 2>/dev/null
    fi
    gpioset -l -c "$chip" "$line=$want" &
    gpio_pid=$!
    gpio_state=$want
}

trap 'if [ -n "$gpio_pid" ]; then kill "$gpio_pid" 2>/dev/null; fi; exit 0' INT TERM

echo "fan on pin 32 ($chip line $line): on above ${load_on}%, off below ${load_off}%, minimum ${min_on}s"
[ "$temp_force" -gt 0 ] && echo "forced on from $((temp_force / 1000)) °C regardless of load"

set_fan 0
sample_cpu           # first sample only primes the counters
running=0
started=0

while :; do
    sleep "$poll"
    sample_cpu
    temp=$(hottest)

    hot=0
    if [ "$temp_force" -gt 0 ] && [ "$temp" -ge "$temp_force" ]; then hot=1; fi

    now=$(date +%s)
    if [ "$running" -eq 0 ]; then
        if [ "$busy" -ge "$load_on" ] || [ "$hot" -eq 1 ]; then
            running=1
            started=$now
            echo "on: cpu ${busy}%, $((temp / 1000)) °C$([ "$hot" -eq 1 ] && echo ' (temperature)')"
        fi
    elif [ "$busy" -lt "$load_off" ] && [ "$hot" -eq 0 ] && [ $((now - started)) -ge "$min_on" ]; then
        running=0
        echo "off: cpu ${busy}%, $((temp / 1000)) °C"
    fi

    set_fan "$running"
done
