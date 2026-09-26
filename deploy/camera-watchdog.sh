#!/usr/bin/env bash
# Bring the USB camera back when it has dropped off the bus.
#
# On 25 September at 23:52 the camera fell off usb1-port1 as a photo began, and
# the kernel could not enable the port again ("Cannot enable. Maybe the USB cable
# is bad?"). After one failed power cycle the hub driver gives up and waits for a
# new connect event, so photos stopped for two and a half hours until the camera
# was replugged by hand. No kernel setting makes it try again.
#
# This watchdog does the retrying and does nothing while the camera is present.
# When it is missing, it first switches the port off and on, then rebinds the
# platform driver that owns the port's controller and its VBUS regulator, which
# is the closest software gets to pulling the plug. It logs which step brought
# the camera back, so the next failure shows what works.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root'; exit 1; }

CAMERA_ID=${CAMERA_ID:-32e6:9221}
# The USB 3 socket: the only port of the xHCI root hub, behind the sunxi DWC3
# glue device 12.usbc2, which also holds the usb1-vbus regulator.
PORT=${CAMERA_PORT:-/sys/bus/usb/devices/1-0:1.0/usb1-port1}
CONTROLLER=${CAMERA_CONTROLLER:-12.usbc2}
DRIVER=${CAMERA_DRIVER:-/sys/bus/platform/drivers/sunxi-plat-dwc3}
CONFIRM_SECONDS=${CAMERA_CONFIRM:-20}
MIN_INTERVAL=${CAMERA_MIN_INTERVAL:-1800}   # a camera unplugged on purpose is retried this rarely
STATE=/run/camera-watchdog
DRY_RUN=${WATCHDOG_DRY_RUN:-0}

present() {
    local device
    for device in /sys/bus/usb/devices/*; do
        [ -f "$device/idVendor" ] || continue
        [ "$(cat "$device/idVendor"):$(cat "$device/idProduct")" = "$CAMERA_ID" ] && return 0
    done
    return 1
}

wait_for_camera() {
    local i
    for ((i = 0; i < $1; i++)); do
        present && return 0
        sleep 1
    done
    return 1
}

# Rebinding drops everything plugged into the controller, so it is done only
# while nothing but its root hubs hangs off it.
controller_is_empty() {
    local path
    path=$(readlink -f "/sys/bus/platform/devices/$CONTROLLER")
    ! find "$path" -path '*/usb[0-9]*/[0-9]*-[0-9]*/idVendor' 2>/dev/null | grep -q .
}

mkdir -p "$STATE"

present && exit 0

now=$(date +%s)
last=$(cat "$STATE/last-attempt" 2>/dev/null || echo 0)
[ $((now - last)) -lt "$MIN_INTERVAL" ] && exit 0

# A camera being replugged is missing for a second or two; do not fight it.
sleep "$CONFIRM_SECONDS"
present && { echo "camera $CAMERA_ID was missing and came back on its own"; exit 0; }

if [ "$DRY_RUN" != 0 ]; then
    echo "dry run: camera $CAMERA_ID is not on the USB bus; $(basename "$PORT") would be switched off and on, then $CONTROLLER rebound"
    exit 0
fi
echo "$now" > "$STATE/last-attempt"

if [ -e "$PORT/disable" ]; then
    echo "camera $CAMERA_ID is not on the USB bus; switching $(basename "$PORT") off and on"
    echo 1 > "$PORT/disable"
    sleep 2
    echo 0 > "$PORT/disable"
    wait_for_camera 15 && { echo "the camera is back after the port power cycle"; exit 0; }
else
    echo "camera $CAMERA_ID is not on the USB bus, and $(basename "$PORT") does not exist"
fi

if ! controller_is_empty; then
    echo "other devices are plugged into $CONTROLLER; not rebinding it"
    exit 1
fi
echo "the camera is still missing; rebinding $CONTROLLER"
echo "$CONTROLLER" > "$DRIVER/unbind" 2>/dev/null
sleep 3
if ! echo "$CONTROLLER" > "$DRIVER/bind"; then
    echo "binding $CONTROLLER again failed; the port stays dead until a reboot"
    exit 1
fi
wait_for_camera 20 && { echo "the camera is back after rebinding $CONTROLLER"; exit 0; }
echo "the camera is still missing; it has to be replugged by hand"
exit 1
