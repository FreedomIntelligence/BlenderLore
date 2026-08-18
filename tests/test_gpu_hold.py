from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gpu_hold


class GpuHoldTests(unittest.TestCase):
    def fake_driver(self) -> gpu_hold.CudaDriver:
        driver = gpu_hold.CudaDriver.__new__(gpu_hold.CudaDriver)
        driver.cu_device_get = lambda pointer, gpu: (
            setattr(pointer._obj, "value", gpu) or 0
        )
        driver.cu_ctx_create = lambda pointer, _flags, device: (
            setattr(pointer._obj, "value", device + 100) or 0
        )
        driver.cu_mem_alloc = lambda pointer, size: (
            setattr(pointer._obj, "value", size + 1000) or 0
        )
        driver.cu_ctx_set_current = lambda _context: 0
        driver.cu_memset_d8 = lambda _pointer, _value, _size: 0
        driver.cu_ctx_synchronize = lambda: 0
        driver.cu_mem_free = lambda _pointer: 0
        driver.cu_ctx_destroy = lambda _context: 0
        return driver

    def test_driver_api_holder_allocates_without_architecture_specific_kernels(self) -> None:
        driver = self.fake_driver()
        context, pointer = driver.allocate(2, 4096)
        self.assertIsInstance(context, ctypes.c_void_p)
        self.assertEqual(context.value, 102)
        self.assertEqual(pointer, 5096)
        driver.touch(context, pointer, 7, 1024)
        driver.release(context, pointer)

    def test_positive_configuration_rejects_zero(self) -> None:
        with self.assertRaises(ValueError):
            gpu_hold.positive_int("MISSING_TEST_VALUE", 0)
        with self.assertRaises(ValueError):
            gpu_hold.positive_float("MISSING_TEST_VALUE", 0)


if __name__ == "__main__":
    unittest.main()
