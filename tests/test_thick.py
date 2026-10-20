import math
import numpy as np
from pipeline.thick import W_MIN_G, W_MAX_G, thick_bytes, grams_of_byte


def test_bytes():
    kg = np.array([0, 0.0005, 0.001, 0.01, 0.027, 0.1, 1.0, 5.0, 20.0, np.nan, -0.002])
    out = thick_bytes(kg)
    assert out.dtype == np.uint8
    assert out.tolist() == [0, 0, 1, 69, 99, 138, 206, 254, 254, 255, 0]
    assert (W_MIN_G, W_MAX_G) == (1.0, 5000.0)
    assert math.isclose(grams_of_byte(99), 27.090, abs_tol=1e-3)
    assert grams_of_byte(1) == 1.0
    assert grams_of_byte(254) == 5000.0
    assert grams_of_byte(0) == 0
    assert math.isnan(grams_of_byte(255))
