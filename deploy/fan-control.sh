#!/usr/bin/env bash
# Switches the case fan on and off by SoC temperature. On/off only, no PWM.
#
# Temperature, not CPU load: load only hints at heat, and a board with the fan
# stopped settles near 50 °C even when idle. The fan starts above FAN_TEMP_ON and
# stops below FAN_TEMP_OFF; the gap and a minimum run time keep it from chattering.
#
# The fan is a Raspberry Pi Active Cooler on header pin 32 (PD5). Its PWM input
# is active high: driven high it spins, driven low it stops. Measured on the
# board on 17 September 2026 by holding each level; see docs/PINOUT.md for the
# wiring and for why a 3.3 V pin can drive it directly.
#
# gpioset owns a line only while it runs, so the value is held by a background
# gpioset that is replaced whenever the state changes. A released line is pulled
# up inside the fan and it runs at full speed, which is the failure this should have.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: it opens /dev/gpiochip0'; exit 1; }
command -v gpioset >/dev/null || { echo 'gpioset is missing: apt install gpiod'; exit 1; }

chip=${FAN_CHIP:-gpiochip0}
line=${FAN_LINE:-101}           # PD5: bank D is the fourth, so 3 * 32 + 5

temp_on=${FAN_TEMP_ON:-55000}   # start above this, millidegrees as in /sys/class/thermal
temp_off=${FAN_TEMP_OFF:-48000} # stop below this; the gap is the hysteresis
min_on=${FAN_MIN_ON:-60}        # once started, keep running this long, seconds
poll=${FAN_POLL:-5}             # seconds between readings
zones=${FAN_ZONES:-'cpub_thermal_zone cpul_thermal_zone gpu_thermal_zone'}

if [ "$temp_off" -ge "$temp_on" ]; then
    echo "FAN_TEMP_OFF ($temp_off) must be below FAN_TEMP_ON ($temp_on)" >&2
    exit 1
fi

temp_files=()
for zone in /sys/class/thermal/thermal_zone*; do
    type=$(cat "$zone/type" 2>/dev/null) || continue
    case " $zones " in *" $type "*) temp_files+=("$zone/temp");; esac
done
if [ ${#temp_files[@]} -eq 0 ]; then
    # Without a temperature the fan cannot be switched safely: exit and let the
    # released line run it at full speed.
    echo "none of the thermal zones exist: $zones" >&2
    exit 1
fi

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
set_fan() {   # 1 = spinning, 0 = stopped; the input is active high, so no -l
    local want=$1
    [ "$gpio_state" = "$want" ] && return
    if [ -n "$gpio_pid" ]; then
        kill "$gpio_pid" 2>/dev/null
        wait "$gpio_pid" 2>/dev/null
    fi
    gpioset -c "$chip" "$line=$want" &
    gpio_pid=$!
    gpio_state=$want
}

trap 'if [ -n "$gpio_pid" ]; then kill "$gpio_pid" 2>/dev/null; fi; exit 0' INT TERM

echo "fan on pin 32 ($chip line $line): on above $((temp_on / 1000)) °C," \
     "off below $((temp_off / 1000)) °C, minimum ${min_on}s"

running=0
started=0
set_fan 0

while :; do
    temp=$(hottest)
    now=$(date +%s)
    if [ "$running" -eq 0 ]; then
        if [ "$temp" -gt "$temp_on" ]; then
            running=1
            started=$now
            echo "on: $((temp / 1000)) °C"
        fi
    elif [ "$temp" -lt "$temp_off" ] && [ $((now - started)) -ge "$min_on" ]; then
        running=0
        echo "off: $((temp / 1000)) °C after $((now - started))s"
    fi
    set_fan "$running"
    sleep "$poll"
done
