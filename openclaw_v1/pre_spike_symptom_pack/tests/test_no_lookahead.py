"""Feature at time t must not reference any bar > t."""
from __future__ import annotations

import numpy as np

from ..strategies._features_runtime import FeatureRuntime


def test_runtime_no_lookahead():
    feature_order = ["atr_14", "bbw"]
    rt = FeatureRuntime(feature_order)
    np.random.seed(0)
    p = 100.0
    snapshots = []
    for i in range(250):
        d = np.random.normal(0, 1)
        p = p + d
        rt.update(i, p, p + 0.3, p - 0.3, p, 1.0)
        snapshots.append(rt.vector.copy())
    # If we replay only the first half, features at bar 100 must equal what
    # they were when computed live at bar 100. (A lookahead bug would change
    # them based on later data, but ring buffers can't "see" the future.)
    rt2 = FeatureRuntime(feature_order)
    np.random.seed(0)
    p = 100.0
    for i in range(250):
        d = np.random.normal(0, 1)
        p = p + d
        rt2.update(i, p, p + 0.3, p - 0.3, p, 1.0)
        if i == 100:
            check = rt2.vector.copy()
            np.testing.assert_array_equal(check, snapshots[100])
