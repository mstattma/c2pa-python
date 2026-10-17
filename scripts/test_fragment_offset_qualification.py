"""Pure stdlib tests: never import c2pa or execute Cargo."""

import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

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

    def test_windows_fork_applicability_is_exact_and_recorded(self):
        skipped = ("skipped", "pytest.skip", "requires os.fork")
        adjustments = []
        with patch.object(q, "sys", SimpleNamespace(platform="win32")), patch.object(q, "os", SimpleNamespace()):
            self.assertTrue(q.compare({q.FORK_TEST_ID: q.FORK_BASELINE_FAILURE}, {q.FORK_TEST_ID: skipped}))
            self.assertEqual(q.compare({q.FORK_TEST_ID: q.FORK_BASELINE_FAILURE},
                                       {q.FORK_TEST_ID: skipped}, platform_adjustments=adjustments, context="native"), [])
        self.assertEqual(adjustments, [{"test_id": q.FORK_TEST_ID, "platform": "win32",
                                       "context": "native", "baseline": q.FORK_BASELINE_FAILURE, "candidate": skipped}])

    def test_fork_applicability_does_not_relax_other_comparisons(self):
        skipped = ("skipped", "pytest.skip", "requires os.fork")
        for platform, os_module, key, previous, outcome in (
                ("linux", SimpleNamespace(), q.FORK_TEST_ID, q.FORK_BASELINE_FAILURE, skipped),
                ("win32", SimpleNamespace(fork=object()), q.FORK_TEST_ID, q.FORK_BASELINE_FAILURE, skipped),
                ("win32", SimpleNamespace(), "other::test", q.FORK_BASELINE_FAILURE, skipped),
                ("win32", SimpleNamespace(), q.FORK_TEST_ID, ("passed", "", ""), skipped),
                ("win32", SimpleNamespace(), q.FORK_TEST_ID, ("failure", "builtins.AssertionError", "different failure"), skipped),
                ("win32", SimpleNamespace(), q.FORK_TEST_ID, q.FORK_BASELINE_FAILURE, ("skipped", "pytest.skip", "other reason")),
                ("win32", SimpleNamespace(), q.FORK_TEST_ID, q.FORK_BASELINE_FAILURE, ("skipped", "other.type", "requires os.fork"))):
            with self.subTest(platform=platform, key=key, previous=previous, outcome=outcome):
                adjustments = []
                with patch.object(q, "sys", SimpleNamespace(platform=platform)), patch.object(q, "os", os_module):
                    self.assertTrue(q.compare({key: previous}, {key: outcome}, platform_adjustments=adjustments, context="native"))
                self.assertEqual(adjustments, [])

    def test_required_focused_ids_match_source_without_native_imports(self):
        source = Path(q.__file__).resolve().parents[1] / "tests/test_fragment_at_offset.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        ids = {f"tests.test_fragment_at_offset.{cls.name}::{method.name}"
               for cls in tree.body if isinstance(cls, ast.ClassDef)
               for method in cls.body if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")}
        self.assertEqual(len(q.REQUIRED_OFFSET_TEST_IDS), 18)
        self.assertEqual(len(q.BINDING_OFFSET_TEST_IDS), 11)
        self.assertEqual(len(q.NATIVE_OFFSET_TEST_IDS), 7)
        self.assertLessEqual(q.REQUIRED_OFFSET_TEST_IDS, ids)

    def test_focused_required_ids_cannot_be_replaced_at_constant_count(self):
        report = dict.fromkeys(q.REQUIRED_OFFSET_TEST_IDS, ("passed", "", ""))
        removed = sorted(q.REQUIRED_OFFSET_TEST_IDS)[0]
        del report[removed]
        report["unrelated.T::test_other"] = ("passed", "", "")
        self.assertEqual(len(report), 18)
        self.assertEqual(q.focused_problems(report), [f"Missing required focused test: {removed}"])

    def test_focused_allows_extra_passes_but_no_extra_failure_or_skip(self):
        report = dict.fromkeys(q.REQUIRED_OFFSET_TEST_IDS, ("passed", "", ""))
        self.assertEqual(q.focused_problems(report), [])
        report["future.T::test_extra"] = ("passed", "", "")
        self.assertEqual(q.focused_problems(report), [])
        for key in (sorted(q.REQUIRED_OFFSET_TEST_IDS)[0], "future.T::test_extra"):
            for outcome in (("failure", "AssertionError", "bad"), ("skipped", "pytest.skip", "unsupported")):
                with self.subTest(key=key, outcome=outcome):
                    self.assertTrue(q.focused_problems({**report, key: outcome}))

    def test_stock_focused_requires_binding_passes_and_exact_native_skips(self):
        report = dict.fromkeys(q.BINDING_OFFSET_TEST_IDS, ("passed", "", ""))
        report.update(dict.fromkeys(q.NATIVE_OFFSET_TEST_IDS, q.STOCK_NATIVE_SKIP))
        self.assertEqual(q.focused_problems(report, stock=True), [])
        self.assertEqual(q.focused_problems({**report, "future.T::test_extra": ("passed", "", "")}, stock=True), [])
        native_id = sorted(q.NATIVE_OFFSET_TEST_IDS)[0]
        binding_id = sorted(q.BINDING_OFFSET_TEST_IDS)[0]
        for key, outcome in ((binding_id, q.STOCK_NATIVE_SKIP),
                             (native_id, ("skipped", "pytest.skip", "wrong reason")),
                             (native_id, ("skipped", "other.type", q.STOCK_NATIVE_SKIP[2])),
                             (native_id, ("passed", "", "")),
                             ("future.T::test_extra", q.STOCK_NATIVE_SKIP)):
            with self.subTest(key=key, outcome=outcome):
                self.assertTrue(q.focused_problems({**report, key: outcome}, stock=True))
        del report[native_id]
        report["unrelated.T::test_other"] = ("passed", "", "")
        self.assertEqual(len(report), 18)
        self.assertTrue(q.focused_problems(report, stock=True))

    def test_fork_guard_is_per_test_and_preserves_unix_applicability(self):
        source = Path(q.__file__).resolve().parents[1] / "tests/test_unit_tests_threaded.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "TestForkedChildDoesNotDeadlock")
        self.assertEqual(cls.decorator_list, [])
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "test_parent_copy_unaffected")
        module = ast.Module(body=[method], type_ignores=[])
        for supports_fork in (False, True):
            namespace = {"unittest": unittest, "os": SimpleNamespace(**({"fork": object()} if supports_fork else {}))}
            exec(compile(module, str(source), "exec"), namespace)
            function = namespace[method.name]
            self.assertEqual(getattr(function, "__unittest_skip__", False), not supports_fork)
            if not supports_fork:
                self.assertEqual(function.__unittest_skip_why__, "requires os.fork")

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
        for scenario in ("unchanged", "native-failure", "native-offset-skip", "stock-failure", "stock-new-skip", "stock-missing-common",
                         "replace-required-id", "stock-binding-skip", "stock-native-wrong-reason",
                         "stock-baseline-unexpected-failure", "windows-fork"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
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
                    if focused:
                        report = dict.fromkeys(q.REQUIRED_OFFSET_TEST_IDS, ("passed", "", ""))
                        if label.startswith("stock-"):
                            report.update(dict.fromkeys(q.NATIVE_OFFSET_TEST_IDS, q.STOCK_NATIVE_SKIP))
                        native_id = sorted(q.NATIVE_OFFSET_TEST_IDS)[0]
                        if scenario == "native-offset-skip" and label.startswith("candidate-"):
                            report[native_id] = q.STOCK_NATIVE_SKIP
                        if scenario == "replace-required-id" and label.startswith("candidate-"):
                            del report[native_id]
                            report["unrelated.T::test_extra"] = ("passed", "", "")
                            self.assertEqual(len(report), 18)
                        if scenario == "stock-binding-skip" and label.startswith("stock-"):
                            report[sorted(q.BINDING_OFFSET_TEST_IDS)[0]] = q.STOCK_NATIVE_SKIP
                        if scenario == "stock-native-wrong-reason" and label.startswith("stock-"):
                            report[native_id] = ("skipped", "pytest.skip", "wrong reason")
                    else:
                        report = {"T::test_common": ("passed", "", "")}
                        if label.startswith(("baseline-", "candidate-")):
                            message = "changed" if scenario == "native-failure" and label.startswith("candidate-") else "baseline failure"
                            report["T::test_common"] = ("failure", "AssertionError", message)
                        if label == "stock-test_unit_tests":
                            if scenario == "stock-failure":
                                report["T::test_common"] = ("failure", "AssertionError", "stock failure")
                            if scenario == "stock-new-skip":
                                report["T::test_common"] = ("skipped", "pytest.skip", "hidden regression")
                            if scenario == "stock-missing-common":
                                report = {"T::test_unrelated": ("passed", "", "")}
                        if scenario == "stock-baseline-unexpected-failure" and label == "stock-baseline-test_unit_tests":
                            report["T::test_common"] = ("failure", "AssertionError", "unexpected baseline failure")
                        if scenario == "windows-fork" and label.endswith("test_unit_tests_threaded"):
                            report[q.FORK_TEST_ID] = (q.FORK_BASELINE_FAILURE if label.startswith(("baseline-", "stock-baseline-"))
                                                      else ("skipped", "pytest.skip", "requires os.fork"))
                    xml = ET.Element("testsuite")
                    for key, (kind, failure_type, message) in report.items():
                        classname, name = key.split("::", 1)
                        case = ET.SubElement(xml, "testcase", classname=classname, name=name)
                        if kind != "passed":
                            ET.SubElement(case, kind, type=failure_type, message=message)
                    ET.ElementTree(xml).write(evidence / (label + ".xml"), encoding="utf-8")
                    return int(any(outcome[0] in ("failure", "error") for outcome in report.values()))

                with patch.object(q, "ROOT", root), patch.object(q, "EVIDENCE", evidence):
                    with patch.object(q, "command", side_effect=fake_command):
                        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(root / "summary.md")}), patch("builtins.print"):
                            sys_module = SimpleNamespace(platform="win32" if scenario == "windows-fork" else "linux", executable=q.sys.executable)
                            with patch.object(q, "sys", sys_module), patch.object(q, "os", SimpleNamespace(environ=os.environ)):
                                self.assertEqual(q.qualify(), int(scenario not in ("unchanged", "windows-fork")))
                summary = (evidence / "summary.md").read_text(encoding="utf-8")
                comparison = json.loads((evidence / "comparison.json").read_text(encoding="utf-8"))
                if scenario == "windows-fork":
                    self.assertEqual({entry["context"] for entry in comparison["platform_adjustments"]}, {"stock", "native"})
                    self.assertEqual(len(comparison["platform_adjustments"]), 2)
                    self.assertEqual(comparison["exit_codes"]["stock-baseline-test_unit_tests_threaded"], 1)
                    self.assertEqual(comparison["exit_codes"]["stock-test_unit_tests_threaded"], 0)
                else:
                    self.assertEqual(comparison["platform_adjustments"], [])
                if scenario in ("stock-new-skip", "stock-missing-common"):
                    self.assertEqual(comparison["exit_codes"]["stock-test_unit_tests"], 0)
                    self.assertTrue(comparison["comparisons"]["stock"])
                self.assertIn("Candidate full-suite status: **FAIL**", summary)
                self.assertIn(f"No-new-regressions: **{'FAIL' if scenario in ('native-failure', 'stock-failure', 'stock-new-skip', 'stock-missing-common') else 'PASS'}**", summary)
                self.assertIn(f"Stock 0.91 compatibility: **{'FAIL' if scenario.startswith('stock-') else 'PASS'}**", summary)
                self.assertEqual(len(seen), 10)
                self.assertEqual(set(comparison["lanes"]), {"stock-baseline", "stock", "baseline", "candidate"})
                for label, args, env in seen:
                    if label.startswith("candidate-"):
                        self.assertEqual(env["C2PA_FRAGMENT_OFFSET_REQUIRED"], "1")
                    if label.startswith("stock-"):
                        self.assertNotIn("C2PA_PREFLIGHT_RUN", env)
                        self.assertNotIn("C2PA_FRAGMENT_OFFSET_REQUIRED", env)
                        self.assertEqual(env["C2PA_LIBRARY_NAME"], str(stock))
                        self.assertEqual(args[5], q.digest(stock))
                    else:
                        self.assertEqual(env["C2PA_PREFLIGHT_RUN"], "1")
                        self.assertEqual(env["C2PA_LIBRARY_NAME"], str(native))
                        self.assertEqual(args[5], q.digest(native))
                    source = root / ("baseline" if label.startswith(("stock-baseline-", "baseline-")) else "candidate")
                    self.assertEqual(args[3], str(source))
                    self.assertEqual(env["PYTHONPATH"], str(source / "src"))

    def test_loaded_provenance_rejects_fallback_without_native_imports(self):
        fake = SimpleNamespace(_lib=SimpleNamespace(_name="relative-fallback.so"))
        package = SimpleNamespace(c2pa=fake)
        with patch.dict("sys.modules", {"c2pa": package, "c2pa.c2pa": fake, "pytest": SimpleNamespace()}):
            with self.assertRaisesRegex(RuntimeError, "Wrong loaded native"):
                q.suite(Path("source"), Path("/expected/library"), "hash", "version", "test.py", "test")


if __name__ == "__main__":
    unittest.main()
