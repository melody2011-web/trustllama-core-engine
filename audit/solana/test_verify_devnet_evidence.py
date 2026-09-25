import importlib.util
import pathlib
import unittest

MODULE = pathlib.Path(__file__).with_name("verify_devnet_evidence.py")
SPEC = importlib.util.spec_from_file_location("devnet_evidence", MODULE)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

IDS = {
    "adapter": "11111111111111111111111111111112",
    "hook": "11111111111111111111111111111113",
    "tlamaMint": "11111111111111111111111111111114",
    "tlamaVault": "11111111111111111111111111111115",
    "wsolVault": "11111111111111111111111111111116",
    "poolAuthority": "11111111111111111111111111111117",
}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"sourceCommit": "a" * 40, "artifacts": {
            "tlama_pool_adapter.so": "b" * 64, "tlama_transfer_hook.so": "c" * 64},
            "ids": dict(IDS)}
        self.evidence = {
            "schemaVersion": 1, "cluster": "devnet",
            "manifest": {"sourceCommit": "a" * 40,
                         "artifactHashes": dict(self.manifest["artifacts"]),
                         "programIds": {"adapter": IDS["adapter"], "hook": IDS["hook"]}},
            "signatures": {name: {"signature": ("1" * 63) + verifier.artifacts.BASE58_ALPHABET[index + 1],
                                  "slot": 1,
                                  "confirmationStatus": "finalized",
                                  "outcome": ("rejected" if name in verifier.REJECTIONS else "successful")}
                           for index, name in enumerate(verifier.SCENARIOS)},
            "accountSnapshots": {name: {"address": IDS[name], "owner": {
                                            "adapter": verifier.BPF_LOADER,
                                            "hook": verifier.BPF_LOADER,
                                            "tlamaMint": verifier.TOKEN_2022,
                                            "tlamaVault": verifier.TOKEN_2022,
                                            "wsolVault": verifier.TOKEN,
                                            "poolAuthority": verifier.SYSTEM_PROGRAM,
                                        }[name],
                                        "lamports": 0, "dataSha256": "d" * 64,
                                        "slot": 1, "confirmationStatus": "finalized"}
                                 for name in verifier.SNAPSHOTS},
            "scenarios": {name: True for name in verifier.SCENARIOS},
        }
        self.evidence["accountSnapshots"]["adapter"]["dataSha256"] = "b" * 64
        self.evidence["accountSnapshots"]["hook"]["dataSha256"] = "c" * 64
        self.original = verifier.artifacts.verify
        verifier.artifacts.verify = lambda *args: None

    def tearDown(self):
        verifier.artifacts.verify = self.original

    def test_accepts_complete_public_evidence(self):
        verifier.verify(self.evidence, self.manifest, pathlib.Path("."))

    def test_rejects_secrets_and_nonfinalized_results(self):
        self.evidence["privateKey"] = "not allowed"
        with self.assertRaisesRegex(SystemExit, "secret-like"):
            verifier.verify(self.evidence, self.manifest, pathlib.Path("."))
        del self.evidence["privateKey"]
        self.evidence["signatures"]["buy"]["confirmationStatus"] = "confirmed"
        with self.assertRaisesRegex(SystemExit, "not finalized"):
            verifier.verify(self.evidence, self.manifest, pathlib.Path("."))

    def test_rejects_missing_scenario(self):
        del self.evidence["scenarios"]["sell"]
        with self.assertRaisesRegex(SystemExit, "scenarios must contain exactly"):
            verifier.verify(self.evidence, self.manifest, pathlib.Path("."))

    def test_rejects_false_rejection_outcome(self):
        self.evidence["signatures"]["capRejection"]["outcome"] = "successful"
        with self.assertRaisesRegex(SystemExit, "outcome must be rejected"):
            verifier.verify(self.evidence, self.manifest, pathlib.Path("."))

    def test_rejects_reused_signature(self):
        self.evidence["signatures"]["sell"]["signature"] = self.evidence["signatures"]["buy"]["signature"]
        with self.assertRaisesRegex(SystemExit, "distinct signature"):
            verifier.verify(self.evidence, self.manifest, pathlib.Path("."))

    def test_rejects_noncanonical_rpc(self):
        with self.assertRaisesRegex(SystemExit, "canonical Solana Devnet"):
            verifier.verify_rpc(self.evidence, "https://example.com")


if __name__ == "__main__":
    unittest.main()