"""BMP280 pressure and temperature over /dev/i2c-N, standard library only.

The kernel has no bmp280 driver and no IIO subsystem, so the sensor is driven
from userspace. Unlike the DHT11 there is nothing time-critical here: the I2C
controller clocks the bus, and each read is one combined transaction (write the
register address, repeated start, read) through the I2C_RDWR ioctl. The ioctl
number and the i2c_msg layout were checked by compiling against
<linux/i2c-dev.h> on the board: i2c_msg is 16 bytes with the buffer at offset 8.

Each reading is one forced-mode measurement: temperature oversampled x2,
pressure x16, IIR filter off. Compensation uses the floating-point formulas
from the Bosch BMP280 datasheet, section 8.1.
"""
import argparse
import ctypes
import os
import struct
import time

I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

REG_CALIBRATION = 0x88    # 24 bytes, dig_T1 .. dig_P9
REG_ID = 0xD0
REG_STATUS = 0xF3
REG_CTRL_MEAS = 0xF4
REG_CONFIG = 0xF5
REG_DATA = 0xF7           # press_msb .. temp_xlsb, 6 bytes

CHIP_ID = 0x58            # 0x60 would be a BME280, which lays out data differently
STATUS_MEASURING = 0x08
CTRL_FORCED = (0b010 << 5) | (0b101 << 2) | 0b01   # osrs_t x2, osrs_p x16, forced
MEASURE_SECONDS = 0.05    # datasheet maximum for these settings is 43.2 ms
CALIBRATION = struct.Struct("<HhhHhhhhhhhh")


class _Message(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]


class _Transfer(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_Message)), ("nmsgs", ctypes.c_uint32)]


_libc = ctypes.CDLL(None, use_errno=True)


class Bus:
    """One device on one I2C bus."""

    def __init__(self, bus=0, address=0x76):
        self.address = address
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR | os.O_CLOEXEC)

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _transfer(self, *messages):
        array = (_Message * len(messages))(*messages)
        transfer = _Transfer(array, len(messages))
        if _libc.ioctl(self.fd, I2C_RDWR, ctypes.byref(transfer)) < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def read(self, register, length):
        out = (ctypes.c_uint8 * 1)(register)
        into = (ctypes.c_uint8 * length)()
        self._transfer(_Message(self.address, 0, 1, out),
                       _Message(self.address, I2C_M_RD, length, into))
        return bytes(into)

    def write(self, register, value):
        data = (ctypes.c_uint8 * 2)(register, value)
        self._transfer(_Message(self.address, 0, 2, data))


def compensate(calibration, adc_t, adc_p):
    """Datasheet 8.1: raw 20-bit readings to (degrees C, pascals)."""
    t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = calibration
    var1 = (adc_t / 16384.0 - t1 / 1024.0) * t2
    var2 = (adc_t / 131072.0 - t1 / 8192.0) ** 2 * t3
    t_fine = var1 + var2
    temperature = t_fine / 5120.0

    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * p6 / 32768.0
    var2 = var2 + var1 * p5 * 2.0
    var2 = var2 / 4.0 + p4 * 65536.0
    var1 = (p3 * var1 * var1 / 524288.0 + p2 * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * p1
    if var1 == 0:
        raise ValueError("pressure compensation divides by zero")
    pressure = 1048576.0 - adc_p
    pressure = (pressure - var2 / 4096.0) * 6250.0 / var1
    var1 = p9 * pressure * pressure / 2147483648.0
    var2 = pressure * p8 / 32768.0
    pressure = pressure + (var1 + var2 + p7) / 16.0
    return temperature, pressure


def raw_values(data):
    """Six data bytes to the 20-bit (adc_t, adc_p) pair."""
    adc_p = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
    adc_t = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
    return adc_t, adc_p


def read(bus=0, address=0x76):
    """One forced measurement: (temperature_c, pressure_hpa), or raise."""
    with Bus(bus, address) as device:
        chip = device.read(REG_ID, 1)[0]
        if chip != CHIP_ID:
            raise ValueError(f"chip id {chip:#04x}, expected {CHIP_ID:#04x} (BMP280)")
        calibration = CALIBRATION.unpack(device.read(REG_CALIBRATION, CALIBRATION.size))
        device.write(REG_CONFIG, 0x00)
        device.write(REG_CTRL_MEAS, CTRL_FORCED)
        time.sleep(MEASURE_SECONDS)
        deadline = time.monotonic() + 0.5
        while device.read(REG_STATUS, 1)[0] & STATUS_MEASURING:
            if time.monotonic() > deadline:
                raise ValueError("measurement did not finish")
            time.sleep(0.005)
        adc_t, adc_p = raw_values(device.read(REG_DATA, 6))
    # 0x80000 is what the chip reports for a channel that was skipped or not ready.
    if adc_t == 0x80000 or adc_p == 0x80000:
        raise ValueError("no measurement in the data registers")
    temperature, pascals = compensate(calibration, adc_t, adc_p)
    if not (-40 <= temperature <= 85 and 300 <= pascals / 100 <= 1100):
        raise ValueError("reading outside the sensor's range")
    return temperature, pascals / 100


def main():
    parser = argparse.ArgumentParser(description="Read a BMP280 on an I2C bus.")
    parser.add_argument("--bus", type=int, default=0)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x76)
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()
    ok = 0
    for attempt in range(args.count):
        if attempt:
            time.sleep(1.0)
        try:
            temperature, pressure = read(args.bus, args.address)
        except (OSError, ValueError) as exc:
            print(f"#{attempt + 1}: {type(exc).__name__}: {exc}")
            continue
        ok += 1
        print(f"#{attempt + 1}: temperature {temperature:.2f} °C, pressure {pressure:.2f} hPa")
    print(f"{ok}/{args.count} valid")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
