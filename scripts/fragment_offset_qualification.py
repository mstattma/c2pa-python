"""Non-publishing fork qualification. Native build mode is GitHub Actions only."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET


NATIVE_REPOSITORY = "mstattma/c2pa-rs"
NATIVE_SHA = "20d7c45972976c32ab24d5d4ac33e7be826ce1be"
NATIVE_VERSION = "0.92.0-dev"
FEATURES = "rust_native_crypto,add_thumbnails,http,file_io"
RUST = "1.96.0"
SUITES = ("test_unit_tests.py", "test_unit_tests_threaded.py")
# Reviewed raw JUnit keys: future passing tests may extend, not replace, these IDs.
BINDING_OFFSET_TEST_IDS = frozenset({
    "tests.test_fragment_at_offset.TestFragmentAtOffsetFeatureDetection::test_exported_from_package",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetFeatureDetection::test_false_without_symbols",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetFeatureDetection::test_probe_result_is_used_and_cached",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetFeatureDetection::test_unsupported_raises_before_touching_the_reader",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_from_rejects_invalid_context",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_rejects_aliased_streams_before_allocation",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_rejects_invalid_format_encoding_before_allocation",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_rejects_invalid_format_types_before_allocation",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_rejects_invalid_streams_before_allocation",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_rejects_non_int_offsets",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetValidation::test_rejects_out_of_range_offsets",
})
NATIVE_OFFSET_TEST_IDS = frozenset({
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_from_fails_cleanly_without_a_manifest",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_from_fragment_at_offset_zero",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_maximum_offset_never_verifies",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_native_failure_closes_the_reader",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_replacing_the_fragment_refreshes_results",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_wrong_offset_is_a_mismatch_not_an_error",
    "tests.test_fragment_at_offset.TestFragmentAtOffsetNative::test_zero_offset_matches_with_fragment",
})
REQUIRED_OFFSET_TEST_IDS = BINDING_OFFSET_TEST_IDS | NATIVE_OFFSET_TEST_IDS
STOCK_NATIVE_SKIP = ("skipped", "pytest.skip", "native library lacks c2pa_reader_with_fragment_at_offset")
FORK_TEST_ID = "tests.test_unit_tests_threaded.TestForkedChildDoesNotDeadlock::test_parent_copy_unaffected"
FORK_BASELINE_FAILURE = (
    "failure", "builtins.AssertionError",
    'AssertionError: 1 != 0 : Traceback (most recent call last):\n'
    '  File "<string>", line 11, in <module>\n'
    "AttributeError: module 'os' has no attribute 'fork'",
)
ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def command(args, cwd, name, env=None, test=False):
    """Only pytest's ordinary failure status is deferred for comparison."""
    with (EVIDENCE / name).open("w", encoding="utf-8") as log:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=log,
                                stderr=subprocess.STDOUT, text=True)
    print(f"{name}: exit {result.returncode}", flush=True)
    if result.returncode not in ((0, 1) if test else (0,)):
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.returncode


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def validate_native(path):
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get(
            "GITHUB_REPOSITORY") != "mstattma/c2pa-python":
        raise RuntimeError("Native qualification builds are fork GitHub Actions only")
    if not re.fullmatch(r"[0-9a-f]{40}", NATIVE_SHA):
        raise ValueError("Native SHA must be full lowercase hex")
    if git(path, "remote", "get-url", "origin") != f"https://github.com/{NATIVE_REPOSITORY}":
        raise ValueError("Native repository is not allowlisted")
    if git(path, "rev-parse", "HEAD") != NATIVE_SHA:
        raise ValueError("Native SHA is not allowlisted")
    if git(path, "status", "--porcelain"):
        raise ValueError("Native checkout must be clean before build")
    if git(path, "ls-files", "Cargo.lock") or (path / "Cargo.lock").exists():
        raise ValueError("Expected an untracked, absent native Cargo.lock")
    with (path / "Cargo.toml").open("rb") as file:
        if tomllib.load(file)["workspace"]["package"]["version"] != NATIVE_VERSION:
            raise ValueError("Unexpected native version")


def build():
    native = ROOT / "native"
    validate_native(native)  # Before installing tools or executing any native source.
    record("sources.json", {
        "native_repository": NATIVE_REPOSITORY, "native_sha": NATIVE_SHA,
        "native_version": NATIVE_VERSION, "features": FEATURES,
        "default_features": False, "profile": "release", "rust": RUST,
        "python_head": git(ROOT / "candidate", "rev-parse", "HEAD"),
        "python_base": git(ROOT / "baseline", "rev-parse", "HEAD"),
        "run_id": os.environ["GITHUB_RUN_ID"], "python": sys.version,
    })
    command(["rustup", "toolchain", "install", RUST, "--profile", "minimal"],
            native, "rustup.log")
    command(["rustc", f"+{RUST}", "-vV"], native, "rustc.log")
    cargo = ["cargo", f"+{RUST}"]
    command(cargo + ["--version", "--verbose"], native, "cargo.log")
    command(cargo + ["generate-lockfile"], native, "resolve.log")
    shutil.copy2(native / "Cargo.lock", EVIDENCE / "Cargo.lock")
    lock_digest = digest(native / "Cargo.lock")
    record("resolved-lock.json", {"sha256": lock_digest, "generated_in_job": True})
    flags = ["--locked", "-p", "c2pa-c-ffi", "--no-default-features", "--features", FEATURES]
    command(cargo + ["tree", *flags, "-e", "features"], native, "features.log")
    command(cargo + ["build", "--release", *flags], native, "build.log")
    if digest(native / "Cargo.lock") != lock_digest:
        raise RuntimeError("Resolved lock changed during locked build")
    name = "c2pa_c.dll" if sys.platform == "win32" else "libc2pa_c.so"
    library = EVIDENCE / name
    shutil.copy2(native / "target" / "release" / name, library)
    record("native.json", {"library": str(library), "sha256": digest(library),
                           "resolved_lock_sha256": lock_digest})


def suite(source, library, expected_hash, version, test_file, label):
    # This runs in a fresh process, with PYTHONPATH and the absolute native path set.
    import c2pa.c2pa as module
    import pytest

    loaded = Path(module._lib._name)
    if not loaded.is_absolute() or loaded.resolve() != library:
        raise RuntimeError(f"Wrong loaded native library: {loaded}")
    if digest(loaded) != expected_hash or Path(module.__file__).resolve() != source / "src/c2pa/c2pa.py":
        raise RuntimeError("Native digest or Python module provenance mismatch")
    if version not in module.sdk_version():
        raise RuntimeError(f"Unexpected SDK version: {module.sdk_version()}")
    record(f"{label}-loaded.json", {"module": module.__file__, "library": str(loaded),
                                   "sha256": digest(loaded), "version": module.sdk_version()})

    class FailureTypes:
        # Pytest's JUnit omits exception types; preserve them explicitly rather
        # than considering two different exceptions with the same message equal.
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = (yield).get_result()
            if report.failed and call.excinfo is not None:
                cls = call.excinfo.type
                item.user_properties.append(("exception_type", f"{cls.__module__}.{cls.__qualname__}"))

    return pytest.main([f"tests/{test_file}", "-v", "--tb=long",
                        "-o", "junit_family=xunit2", f"--junitxml={EVIDENCE / (label + '.xml')}"],
                       plugins=[FailureTypes()])


def normalize(message):
    message = re.sub(r"\\+", "/", message)
    for folder in (ROOT / "candidate", ROOT / "baseline", ROOT):
        message = message.replace(folder.as_posix(), "<checkout>")
    temporary = re.escape(Path(tempfile.gettempdir()).resolve().as_posix())
    message = re.sub(temporary + r"/tmp[a-zA-Z0-9_-]{8}(?=[/.\s'\"<>:)]|$)", "<temporary>", message)
    return re.sub(r"0x[0-9a-fA-F]+", "0xADDR", message)


def cases(path):
    result = {}
    for case in ET.parse(path).iter("testcase"):
        key = case.attrib["classname"] + "::" + case.attrib["name"]
        if key in result:
            raise ValueError(f"Duplicate test ID: {key}")
        outcome = ("passed", "", "")
        for kind in ("failure", "error", "skipped"):
            node = case.find(kind)
            if node is not None:
                exception_type = case.find("./properties/property[@name='exception_type']")
                failure_type = node.get("type", "") if exception_type is None else exception_type.get("value", "")
                if kind != "skipped" and not failure_type:
                    raise ValueError(f"Missing exception type for {key}")
                outcome = (kind, failure_type, normalize(node.get("message", "")))
                break
        result[key] = outcome
    if not result:
        raise ValueError(f"No tests executed: {path}")
    return result


def focused_problems(report, *, stock=False):
    problems = [f"Missing required focused test: {key}" for key in REQUIRED_OFFSET_TEST_IDS - report.keys()]
    for key, outcome in report.items():
        expected = STOCK_NATIVE_SKIP if stock and key in NATIVE_OFFSET_TEST_IDS else ("passed", "", "")
        if outcome != expected:
            problems.append(f"Unexpected focused outcome: {key}: {outcome!r}; expected: {expected!r}")
    return sorted(problems)


def compare(baseline, candidate, *, platform_adjustments=None, context=None):
    problems = [f"Missing common test: {key}" for key in baseline.keys() - candidate.keys()]
    for key, outcome in candidate.items():
        previous = baseline.get(key, ("passed", "", ""))
        # The base predates the fork guard. Exempt only its exact Windows
        # missing-fork failure, never a passing test or an unrelated failure.
        if (platform_adjustments is not None and context in ("stock", "native") and sys.platform == "win32"
                and not hasattr(os, "fork") and key == FORK_TEST_ID
                and previous == FORK_BASELINE_FAILURE
                and outcome == ("skipped", "pytest.skip", "requires os.fork")):
            platform_adjustments.append({"test_id": key, "platform": sys.platform, "context": context,
                                         "baseline": previous, "candidate": outcome})
            continue
        if outcome[0] != "passed" and outcome != previous:
            problems.append(f"New/changed outcome: {key}: {outcome!r}; baseline: {previous!r}")
    return sorted(problems)


def qualify():
    candidate, baseline = ROOT / "candidate", ROOT / "baseline"
    native = json.loads((EVIDENCE / "native.json").read_text(encoding="utf-8"))
    library = Path(native["library"]).resolve()
    # Keep ordinary release pins untouched; only ephemeral preflight files differ.
    for source in (candidate, baseline):
        (source / "c2pa-rs-preflight-ref.txt").write_text(f"c2pa-v{NATIVE_VERSION}\n", encoding="utf-8")
    command([sys.executable, "scripts/download_artifacts.py", "c2pa-v0.91.0"],
            candidate, "stock-download.log")
    name = library.name
    stock = list((candidate / "artifacts").glob(f"*/{name}"))
    if len(stock) != 1:
        raise RuntimeError(f"Expected one downloaded stock library, found {stock}")
    stock = stock[0].resolve()
    shutil.copy2(stock, EVIDENCE / ("stock-" + name))
    results, reports, regressions, platform_adjustments = {}, {}, [], []
    lanes = (("stock-baseline", baseline, stock, digest(stock), "0.91.0"),
             ("stock", candidate, stock, digest(stock), "0.91.0"),
             ("baseline", baseline, library, native["sha256"], NATIVE_VERSION),
             ("candidate", candidate, library, native["sha256"], NATIVE_VERSION))
    for lane, source, binary, sha, version in lanes:
        files = SUITES + (("test_fragment_at_offset.py",) if lane in ("stock", "candidate") else ())
        for test_file in files:
            label = lane + "-" + test_file.removesuffix(".py")
            env = dict(os.environ, PYTHONPATH=str(source / "src"),
                       C2PA_LIBRARY_NAME=str(binary), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
            env.pop("C2PA_PREFLIGHT_RUN", None)
            env.pop("C2PA_FRAGMENT_OFFSET_REQUIRED", None)
            if lane in ("baseline", "candidate"):
                env["C2PA_PREFLIGHT_RUN"] = "1"
            if lane == "candidate":
                env["C2PA_FRAGMENT_OFFSET_REQUIRED"] = "1"
            results[label] = command([sys.executable, str(Path(__file__).resolve()), "suite",
                                      str(source), str(binary), sha, version, test_file, label],
                                     source, label + ".log", env, test=True)
            reports[label] = cases(EVIDENCE / (label + ".xml"))
            if (results[label] == 0) != all(v[0] in ("passed", "skipped") for v in reports[label].values()):
                raise RuntimeError(f"Process/JUnit status disagreement: {label}")
    comparisons = {}
    for context, base_lane, pr_lane in (("stock", "stock-baseline", "stock"), ("native", "baseline", "candidate")):
        comparisons[context] = []
        for file in SUITES:
            suffix = "-" + file.removesuffix(".py")
            comparisons[context].extend(compare(reports[base_lane + suffix], reports[pr_lane + suffix],
                                                platform_adjustments=platform_adjustments, context=context))
        regressions.extend(f"{context}: {problem}" for problem in comparisons[context])
    problems = list(regressions)
    focused = {lane: focused_problems(reports[lane + "-test_fragment_at_offset"], stock=lane == "stock")
               for lane in ("stock", "candidate")}
    focused_pass = not focused["candidate"]
    problems.extend(f"{lane} focused: {problem}" for lane, issues in focused.items() for problem in issues)
    # The unmodified stock base may fail only the explicitly recognized fork case.
    allowed_base_failures = {entry["test_id"]: entry["baseline"] for entry in platform_adjustments
                             if entry["context"] == "stock"}
    stock_base_problems = []
    for file in SUITES:
        label = "stock-baseline-" + file.removesuffix(".py")
        stock_base_problems.extend(f"Unexpected stock baseline failure: {key}: {outcome!r}"
                                   for key, outcome in reports[label].items()
                                   if outcome[0] in ("failure", "error") and allowed_base_failures.get(key) != outcome)
    problems.extend(stock_base_problems)
    stock_labels = ["stock-" + file.removesuffix(".py") for file in (*SUITES, "test_fragment_at_offset.py")]
    stock_pass = (all(results[label] == 0 for label in stock_labels)
                  and not comparisons["stock"] and not focused["stock"] and not stock_base_problems)
    if not stock_pass:
        problems.append("Stock 0.91 compatibility failed")
    failures = {label: {key: value for key, value in report.items() if value[0] != "passed"}
                for label, report in reports.items()}
    record("comparison.json", {"exit_codes": results, "nonpasses": failures,
                               "regressions": regressions, "qualification_failures": problems,
                               "comparisons": comparisons, "focused_problems": focused,
                               "focused_requirements": {"required_ids": sorted(REQUIRED_OFFSET_TEST_IDS),
                                                        "stock_native_skip_ids": sorted(NATIVE_OFFSET_TEST_IDS),
                                                        "stock_native_skip": STOCK_NATIVE_SKIP},
                               "lanes": {lane: {"python_source": str(source), "library": str(binary),
                                                "sha256": sha, "version": version}
                                         for lane, source, binary, sha, version in lanes},
                               "platform_adjustments": platform_adjustments})
    full = all(status == 0 for label, status in results.items() if label.startswith("candidate-"))
    summary = (f"## Fragment offset qualification\n\n"
               f"Candidate full-suite status: **{'PASS' if full else 'FAIL'}**\n\n"
               f"No-new-regressions: **{'FAIL' if regressions else 'PASS'}**\n\n"
               f"Stock 0.91 compatibility: **{'PASS' if stock_pass else 'FAIL'}**\n\n"
               f"Required 18 focused offset IDs (all pass, no skips): **{'PASS' if focused_pass else 'FAIL'}**\n\n"
               f"Stock focused IDs (11 pass, 7 exact unsupported skips): **{'FAIL' if focused['stock'] else 'PASS'}**\n\n"
               f"Combined qualification gate: **{'FAIL' if problems else 'PASS'}**\n\n"
               f"Explicit platform applicability adjustments: **{len(platform_adjustments)}**\n\n"
               "Full-suite failures are not waived as full passes. Exact IDs, failure types and "
               "normalized messages (including unchanged baseline failures) are in comparison.json; "
               "raw JUnit, logs and loaded-library evidence are retained.\n\n"
               + "\n".join(f"- {label}: exit {status}" for label, status in results.items()) + "\n\n"
               + "\n".join(f"- `{label} / {key}`: {value!r}"
                           for label, report in failures.items() for key, value in report.items()
                           if value[0] != "skipped") + "\n"
               + "\n".join(f"- Platform applicability ({entry['context']}): `{entry['test_id']}` on {entry['platform']}: "
                           f"{entry['baseline']!r} -> {entry['candidate']!r}"
                           for entry in platform_adjustments) + "\n")
    (EVIDENCE / "summary.md").write_text(summary, encoding="utf-8")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
        file.write(summary)
    print(summary)
    return int(bool(problems))


def main():
    EVIDENCE.mkdir(exist_ok=True)
    if sys.argv[1] == "build":
        build()
        return 0
    if sys.argv[1] == "qualify":
        return qualify()
    if sys.argv[1] == "suite":
        source, library, sha, version, test_file, label = sys.argv[2:]
        return suite(Path(source), Path(library), sha, version, test_file, label)
    raise ValueError("Unknown qualification mode")


if __name__ == "__main__":
    sys.exit(main())
