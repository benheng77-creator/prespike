"""
Tests for Bayesian posterior + KL early stop + conflict score math.
Pure logic — does not call any LLM.
"""

from __future__ import annotations

import math
import unittest

from spot_aggro.ops.llm.consensus import (
    bayes_update, kl_binary, compute_conflict, parse_member_output,
)


class TestBayesUpdate(unittest.TestCase):

    def test_neutral_preserves_prior(self):
        self.assertAlmostEqual(bayes_update(0.5, 0.0, 1.0), 0.5, places=6)

    def test_full_positive_vote_shifts_up(self):
        # With confidence capped at 0.70, a +1.0/1.0 vote shifts to ~0.74
        p = bayes_update(0.5, 1.0, 1.0)
        self.assertGreater(p, 0.7)

    def test_full_negative_vote_shifts_down(self):
        # With confidence capped at 0.70, a -1.0/1.0 vote shifts to ~0.26
        p = bayes_update(0.5, -1.0, 1.0)
        self.assertLess(p, 0.3)

    def test_low_confidence_dampens(self):
        p_high = bayes_update(0.5, 1.0, 1.0)
        p_low  = bayes_update(0.5, 1.0, 0.2)
        self.assertGreater(p_high, p_low)

    def test_compounding_two_aligned(self):
        p1 = bayes_update(0.5, 0.8, 0.9)
        p2 = bayes_update(p1, 0.8, 0.9)
        self.assertGreater(p2, p1)
        self.assertLess(p2, 1.0)


class TestKL(unittest.TestCase):

    def test_identical_zero(self):
        self.assertAlmostEqual(kl_binary(0.5, 0.5), 0.0, places=9)

    def test_diff_positive(self):
        self.assertGreater(kl_binary(0.6, 0.5), 0.0)

    def test_symmetric_pairs_close(self):
        a = kl_binary(0.9, 0.5)
        b = kl_binary(0.1, 0.5)
        self.assertAlmostEqual(a, b, places=6)


class TestConflict(unittest.TestCase):

    def test_aligned_low_conflict(self):
        self.assertLess(compute_conflict([0.8, 0.82, 0.78, 0.81]), 0.1)

    def test_split_votes_high_conflict(self):
        # [-0.9, +0.9] dispersion = 0.9. Above 0.7 → triggers Opus veto.
        self.assertGreater(compute_conflict([0.9, -0.9, 0.8, -0.8]), 0.7)

    def test_moderate_disagree_under_threshold(self):
        # reasonable split that SHOULDN'T trigger veto
        self.assertLess(compute_conflict([0.4, 0.5, 0.3, 0.45]), 0.2)

    def test_one_neutral_amongst_agreers(self):
        # [0.0, 0.7, 0.8] — one abstain, two agree. Shouldn't trigger veto.
        self.assertLess(compute_conflict([0.0, 0.7, 0.8]), 0.7)

    def test_bounded_by_one(self):
        # Score range is [-1, +1], so σ cannot exceed 1.0.
        self.assertLessEqual(compute_conflict([-1.0, 1.0, -1.0, 1.0]), 1.0)

    def test_empty(self):
        self.assertEqual(compute_conflict([]), 0.0)


class TestParseMemberOutput(unittest.TestCase):

    def test_clean_json(self):
        s, c, r = parse_member_output(
            '{"score": 0.7, "confidence": 0.9, "rationale": "strong funding"}'
        )
        self.assertAlmostEqual(s, 0.7)
        self.assertAlmostEqual(c, 0.9)
        self.assertEqual(r, "strong funding")

    def test_with_code_fences(self):
        s, c, _ = parse_member_output(
            '```json\n{"score": -0.5, "confidence": 0.6}\n```'
        )
        self.assertAlmostEqual(s, -0.5)
        self.assertAlmostEqual(c, 0.6)

    def test_with_prose_preamble(self):
        s, c, _ = parse_member_output(
            'Here is my analysis:\n{"score": 0.2, "confidence": 0.3, "rationale": "noisy"}'
        )
        self.assertAlmostEqual(s, 0.2)

    def test_malformed_returns_none(self):
        s, c, _ = parse_member_output('not json at all')
        self.assertIsNone(s)
        self.assertIsNone(c)

    def test_clips_out_of_range(self):
        s, c, _ = parse_member_output(
            '{"score": 5.0, "confidence": 2.0, "rationale": ""}'
        )
        self.assertEqual(s, 1.0)
        self.assertEqual(c, 1.0)


if __name__ == "__main__":
    unittest.main()
