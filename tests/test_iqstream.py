"""iqstream: 12-bit packing, and (when it's built) the real streamer over loopback."""

import numpy as np
import pytest

from atcsdr import iqstream as IQ


def test_pack_unpack_is_exact():
    x = np.concatenate([IQ.test_pattern(0, 10_000),
                        np.array([-2048, 2047, -1, 0, 1, -2047], np.int16)])
    packed = IQ.pack12(x)
    assert len(packed) == 3 * len(x) // 2                 # 3 bytes a sample, not 4
    assert np.array_equal(IQ.unpack12(packed), x)
    assert np.array_equal(IQ._unpack12_numpy(packed), x)


def test_test_pattern_matches_the_c_formula():
    k = np.arange(5)
    x = IQ.test_pattern(0, 5)
    assert list(x[0::2]) == list(((k * 7) & 0xFFF) - 2048)
    assert list(x[1::2]) == list(((k * 13 + 1000) & 0xFFF) - 2048)


@pytest.mark.skipif(not IQ.NATIVE.exists(), reason="build/iqstream-native not built (pi/build_iqstream.sh)")
@pytest.mark.parametrize("conns, bits", [(1, 16), (2, 16), (1, 12), (2, 12)])
def test_streamer_loopback(conns, bits):
    r = IQ.PackedPluto("packed:127.0.0.1", 0, 21.6e6, buffer_size=1 << 15, conns=conns, bits=bits,
                       test_rate=0, local=True, port=5340 + 2 * conns + bits % 2)
    r._skip_until = -1
    try:
        for _ in range(200):
            y = r.read()
            k0 = (r.last_seq + r.stats["lost"]) * (len(y) // 2)
            assert np.array_equal(y, IQ.test_pattern(k0, len(y) // 2))
    finally:
        r.close()
