import importlib.util
import pathlib
import tempfile
import unittest

from solders.pubkey import Pubkey

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "configure_tlama_production_ids.py"
SPEC = importlib.util.spec_from_file_location("configure_tlama_production_ids", SCRIPT)
tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tool)


class ProductionIdsToolTest(unittest.TestCase):
    def valid_values(self):
        adapter = Pubkey.new_unique()
        pool_authority, _ = Pubkey.find_program_address([b"pool-authority"], adapter)
        keys = [adapter, Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique(), pool_authority]
        return dict(zip(tool.KEYS, map(str, keys)))

    def test_accepts_valid_distinct_tuple_with_derived_pda(self):
        values = self.valid_values()
        self.assertEqual(tool.validate(values), values)

    def test_rejects_invalid_address(self):
        values = self.valid_values()
        values["hook"] = "not-a-solana-address"
        with self.assertRaisesRegex(tool.ValidationError, "32-byte base58"):
            tool.validate(values)

    def test_rejects_duplicate_addresses(self):
        values = self.valid_values()
        values["hook"] = values["tlamaMint"]
        with self.assertRaisesRegex(tool.ValidationError, "must be distinct"):
            tool.validate(values)

    def test_rejects_wrong_pool_authority(self):
        values = self.valid_values()
        values["poolAuthority"] = str(Pubkey.new_unique())
        with self.assertRaisesRegex(tool.ValidationError, "does not match"):
            tool.validate(values)

    def test_rejects_disposable_development_address(self):
        values = self.valid_values()
        values["hook"] = next(iter(tool.load_development_values()))
        with self.assertRaisesRegex(tool.ValidationError, "development addresses"):
            tool.validate(values)

    def test_renders_exact_six_line_contract_and_writes_atomically(self):
        values = self.valid_values()
        expected = "".join(f"{key}={values[key]}\n" for key in tool.KEYS)
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "production-ids.txt"
            tool.write_atomically(tool.render(values), output)
            self.assertEqual(output.read_text(encoding="utf-8"), expected)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()