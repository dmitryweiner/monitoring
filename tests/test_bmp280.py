import ctypes

import pytest

from monitoring import bmp280

# Worked example from the Bosch BMP280 datasheet, section 3.12.
DATASHEET_CALIBRATION = (27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)


def test_compensation_matches_the_datasheet_example():
    temperature, pascals = bmp280.compensate(DATASHEET_CALIBRATION, 519888, 415148)
    assert temperature == pytest.approx(25.08, abs=0.01)
    assert pascals == pytest.approx(100653.27, abs=0.1)


def test_raw_values_assemble_twenty_bits():
    # The xlsb register carries its four bits in the top nibble.
    data = bytes([0x65, 0x5A, 0xC0, 0x7E, 0xED, 0x00])
    adc_t, adc_p = bmp280.raw_values(data)
    assert adc_p == (0x65 << 12) | (0x5A << 4) | 0xC
    assert adc_t == (0x7E << 12) | (0xED << 4)


def test_calibration_layout():
    assert bmp280.CALIBRATION.size == 24


def test_transfer_structs_match_the_kernel_header():
    # Checked against <linux/i2c-dev.h> on the board: 16 bytes, buffer at offset 8.
    assert ctypes.sizeof(bmp280._Message) == 16
    assert bmp280._Message.buf.offset == 8
    assert ctypes.sizeof(bmp280._Transfer) == 16


def test_missing_bus_raises_os_error():
    with pytest.raises(FileNotFoundError):
        bmp280.read(bus=99)
