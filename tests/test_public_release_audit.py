import tempfile
import unittest
from pathlib import Path

from scripts.public_release_audit import scan_paths


class PublicReleaseAuditTests(unittest.TestCase):
    def test_detects_private_files_and_high_confidence_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manual_notes.json").write_text("{}", encoding="utf-8")
            (root / "safe.py").write_text(
                "API_KEY = 'AIza" + "A" * 32 + "'\n",
                encoding="utf-8",
            )

            findings = scan_paths(
                root,
                [Path("manual_notes.json"), Path("safe.py")],
            )

        kinds = {finding["kind"] for finding in findings}
        self.assertIn("private-file", kinds)
        self.assertIn("google-api-key", kinds)

    def test_allows_synthetic_demo_content_and_private_filename_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo.py").write_text(
                'NOTES_PATH = BASE / "manual_notes.json"\n'
                '"manual_note": "Synthetic demo note"\n',
                encoding="utf-8",
            )

            findings = scan_paths(root, [Path("demo.py")])

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
