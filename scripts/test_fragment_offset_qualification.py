"""Pure stdlib tests: never import c2pa or execute Cargo."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fragment_offset_qualification as q


class QualificationTests(unittest.TestCase):
    def test_compare_requires_every_common_test(self):
        self.assertTrue(q.compare({"lost": ("passed", "", "")}, {}))

    def test_compare_accepts_identical_baseline_failure_and_fixes(self):
        failure = ("failure", "AssertionError", "expected output")
        self.assertEqual(q.compare({"a": failure}, {"a": failure}), [])
        self.assertEqual(q.compare({"a": failure}, {"a": ("passed", "", "")}), [])

    def test_compare_rejects_new_or_changed_failure_type_message_and_skip(self):
        failure = ("failure", "AssertionError", "expected output")
        for outcome in (("failure", "ValueError", failure[2]),
                        ("failure", failure[1], "different output"),
                        ("skipped", "", "unsupported")):
            with self.subTest(outcome=outcome):
                self.assertTrue(q.compare({"a": failure}, {"a": outcome}))
                self.assertTrue(q.compare({}, {"new": outcome}))

    def test_normalize_only_paths_and_addresses(self):
        with patch.object(q, "ROOT", Path("/work")):
            self.assertEqual(q.normalize("/work/baseline/test.py 0x123abc vs 42"),
                             q.normalize("/work/candidate/test.py 0x456def vs 42"))
            self.assertNotEqual(q.normalize("expected 41"), q.normalize("expected 42"))
        with patch.object(q.tempfile, "gettempdir", return_value="/temp"):
            temporary = Path("/temp").resolve().as_posix()
            self.assertEqual(q.normalize(temporary + "/tmp12_abcd3/file.jpg"), "<temporary>/file.jpg")
            self.assertNotEqual(q.normalize("/temp/real-file.jpg"), "<temporary>")

    def test_command_propagates_errors_and_only_defers_test_failures(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(q, "EVIDENCE", Path(directory)):
            with patch.object(q.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    q.command(["fake"], directory, "build.log")
                self.assertEqual(q.command(["fake"], directory, "test.log", test=True), 1)
                self.assertEqual(run.call_args.kwargs["cwd"], directory)
            with patch.object(q.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
                with self.assertRaises(subprocess.CalledProcessError):
                    q.command(["fake"], directory, "test.log", test=True)

    def test_no_local_native_build(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(q, "git") as git:
            with self.assertRaises(RuntimeError):
                q.validate_native(Path("unused"))
            git.assert_not_called()

    def test_build_cannot_execute_tools_before_validation(self):
        with patch.object(q, "validate_native", side_effect=ValueError("refused")):
            with patch.object(q, "command") as command:
                with self.assertRaises(ValueError):
                    q.build()
                command.assert_not_called()

    def test_native_allowlist_before_source_execution(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "mstattma/c2pa-python"}
        for origin, sha in (("https://github.com/other/c2pa-rs", q.NATIVE_SHA),
                            (f"https://github.com/{q.NATIVE_REPOSITORY}", "f" * 40)):
            with self.subTest(origin=origin, sha=sha), patch.dict(os.environ, env):
                with patch.object(q, "git", side_effect=[origin, sha]):
                    with self.assertRaises(ValueError):
                        q.validate_native(Path("unused"))

    def test_junit_ids_types_messages_and_empty_or_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.xml"
            xml = '<testcase classname="tests.T" name="test_x"><failure type="AssertionError" message="bad 0x123"/></testcase>'
            path.write_text(f"<testsuite>{xml}</testsuite>", encoding="utf-8")
            self.assertEqual(q.cases(path), {"tests.T::test_x": ("failure", "AssertionError", "bad 0xADDR")})
            for body in ("", xml + xml):
                path.write_text(f"<testsuite>{body}</testsuite>", encoding="utf-8")
                with self.assertRaises(ValueError):
                    q.cases(path)

    def test_junit_requires_and_preserves_explicit_exception_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.xml"
            for properties in ("", '<properties><property name="exception_type" value="builtins.AssertionError"/></properties>'):
                path.write_text('<testsuite><testcase classname="T" name="test">'
                                + properties + '<failure message="bad"/></testcase></testsuite>', encoding="utf-8")
                if properties:
                    self.assertEqual(q.cases(path)["T::test"], ("failure", "builtins.AssertionError", "bad"))
                else:
                    with self.assertRaises(ValueError):
                        q.cases(path)

    def test_qualification_keeps_full_failure_distinct_and_enforces_gates(self):
        for changed, skip_offset, stock_fail in ((False, False, False), (True, False, False),
                                                (False, True, False), (False, False, True)):
            with self.subTest(changed=changed, skip_offset=skip_offset, stock_fail=stock_fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                evidence = root / "evidence"
                evidence.mkdir()
                (root / "baseline").mkdir()
                stock_dir = root / "candidate/artifacts/platform"
                stock_dir.mkdir(parents=True)
                stock = stock_dir / "native.bin"
                stock.write_bytes(b"stock")
                native = evidence / "native.bin"
                native.write_bytes(b"candidate")
                (evidence / "native.json").write_text(json.dumps({"library": str(native), "sha256": q.digest(native)}), encoding="utf-8")
                seen = []

                def fake_command(args, cwd, name, env=None, test=False):
                    if not test:
                        return 0
                    label = args[-1]
                    seen.append((label, args, env))
                    focused = label.endswith("test_fragment_at_offset")
                    failing = not focused and (not label.startswith("stock-") or stock_fail)
                    body = ""
                    for index in range(18 if focused else 1):
                        outcome = ""
                        if failing:
                            message = "changed" if changed and label.startswith("candidate-") else "baseline failure"
                            outcome = f'<failure type="AssertionError" message="{message}"/>'
                        if skip_offset and label == "candidate-test_fragment_at_offset" and index == 0:
                            outcome = '<skipped message="unsupported"/>'
                        body += f'<testcase classname="T" name="test_{index}">{outcome}</testcase>'
                    (evidence / (label + ".xml")).write_text(f"<testsuite>{body}</testsuite>", encoding="utf-8")
                    return int(failing)

                with patch.object(q, "ROOT", root), patch.object(q, "EVIDENCE", evidence):
                    with patch.object(q, "command", side_effect=fake_command):
                        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(root / "summary.md")}), patch("builtins.print"):
                            self.assertEqual(q.qualify(), int(changed or skip_offset or stock_fail))
                summary = (evidence / "summary.md").read_text(encoding="utf-8")
                self.assertIn("Candidate full-suite status: **FAIL**", summary)
                self.assertIn(f"No-new-regressions: **{'FAIL' if changed else 'PASS'}**", summary)
                self.assertIn(f"Stock 0.91 compatibility: **{'FAIL' if stock_fail else 'PASS'}**", summary)
                self.assertEqual(len(seen), 8)
                for label, args, env in seen:
                    if label.startswith("candidate-"):
                        self.assertEqual(env["C2PA_FRAGMENT_OFFSET_REQUIRED"], "1")
                    if not label.startswith("stock-"):
                        self.assertEqual(env["C2PA_PREFLIGHT_RUN"], "1")
                        self.assertEqual(env["C2PA_LIBRARY_NAME"], str(native))
                        self.assertEqual(args[5], q.digest(native))

    def test_loaded_provenance_rejects_fallback_without_native_imports(self):
        fake = SimpleNamespace(_lib=SimpleNamespace(_name="relative-fallback.so"))
        package = SimpleNamespace(c2pa=fake)
        with patch.dict("sys.modules", {"c2pa": package, "c2pa.c2pa": fake, "pytest": SimpleNamespace()}):
            with self.assertRaisesRegex(RuntimeError, "Wrong loaded native"):
                q.suite(Path("source"), Path("/expected/library"), "hash", "version", "test.py", "test")


if __name__ == "__main__":
    unittest.main()
