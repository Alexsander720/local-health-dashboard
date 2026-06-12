import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class AtomicStorageTests(unittest.TestCase):
    def load_storage_utils(self):
        spec = importlib.util.find_spec("storage_utils")
        self.assertIsNotNone(spec, "storage_utils module must exist")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_atomic_write_json_replaces_file_without_temp_artifacts(self):
        storage_utils = self.load_storage_utils()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")

            storage_utils.atomic_write_json(path, {"new": "value"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": "value"})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_serialization_failure_preserves_existing_file(self):
        storage_utils = self.load_storage_utils()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            original = '{"safe": true}'
            path.write_text(original, encoding="utf-8")

            with self.assertRaises(TypeError):
                storage_utils.atomic_write_json(path, {"bad": object()})

            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
