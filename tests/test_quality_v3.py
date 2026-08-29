from __future__ import annotations

import sys
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev.quality import QualityDecision, QualityRouter


class QualityRouterTests(unittest.TestCase):
    def test_routing_separates_immediate_phase_and_diagnostic(self) -> None:
        router = QualityRouter()
        self.assertEqual(router.decide({"risk": "LOW", "change_classes": ["implementation"]}), QualityDecision.NONE)
        self.assertEqual(router.decide({"risk": "MEDIUM", "change_classes": ["architecture"]}), QualityDecision.PHASE)
        self.assertEqual(router.decide({"risk": "HIGH", "change_classes": ["implementation"]}), QualityDecision.IMMEDIATE)
        self.assertEqual(
            router.decide(
                {"risk": "LOW", "change_classes": ["implementation"]},
                failure_fingerprints=["same", "same"],
            ),
            QualityDecision.DIAGNOSTIC,
        )
        self.assertEqual(
            router.decide(
                {"risk": "LOW", "change_classes": ["implementation"]},
                failure_fingerprints=["same", "same"], diagnostic_used=True,
            ),
            QualityDecision.NONE,
        )


if __name__ == "__main__":
    unittest.main()
