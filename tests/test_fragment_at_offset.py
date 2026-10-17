# Copyright 2026 Adobe. All rights reserved.
# This file is licensed to you under the Apache License,
# Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
# or the MIT license (http://opensource.org/licenses/MIT),
# at your option.

# Unless required by applicable law or agreed to in writing,
# this software is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR REPRESENTATIONS OF ANY KIND, either express or
# implied. See the LICENSE-MIT and LICENSE-APACHE files for the
# specific language governing permissions and limitations under
# each license.

"""Tests for Reader.with_fragment_at_offset / from_fragment_at_offset.

Two groups:

* Binding behavior that does not need the new native entry point: feature
  detection, argument validation, and that a refused call leaves the reader
  untouched. These run against any native library.
* Native behavior, against a library that exports
  c2pa_reader_with_fragment_at_offset. They are skipped when the loaded
  library lacks it, unless C2PA_FRAGMENT_OFFSET_REQUIRED=1, which turns the
  skip into a failure for CI lanes that build the native library.

The native tests use the signed multi-file DASH fixtures: a multi-file
fragment verifies at offset 0 and is a hash mismatch at any other offset. The
offset arithmetic on single-file layouts is covered by the c2pa-rs tests.
"""

import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import c2pa
import c2pa.c2pa as c2pa_module
from c2pa import C2paError as Error, Context, Reader

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
INIT_PATH = os.path.join(FIXTURES_DIR, "dashinit.mp4")
FRAGMENT_PATH = os.path.join(FIXTURES_DIR, "dash1.m4s")
DEFAULT_TEST_FILE = os.path.join(FIXTURES_DIR, "C.jpg")
BMFF_MISMATCH = "assertion.bmffHash.mismatch"
REQUIRED = os.environ.get("C2PA_FRAGMENT_OFFSET_REQUIRED") == "1"


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _failure_codes(reader):
    store = json.loads(reader.json())
    return [s.get("code") for s in store.get("validation_status") or []]


class TestFragmentAtOffsetFeatureDetection(unittest.TestCase):

    def test_exported_from_package(self):
        self.assertIs(c2pa.has_fragment_at_offset,
                      c2pa_module.has_fragment_at_offset)
        self.assertIn("has_fragment_at_offset", c2pa.__all__)

    def test_false_without_symbols(self):
        with patch.object(
                c2pa_module, "_FRAGMENT_AT_OFFSET_SYMBOLS_AVAILABLE", False):
            self.assertFalse(c2pa.has_fragment_at_offset())

    def test_probe_result_is_used_and_cached(self):
        with patch.object(
                c2pa_module, "_FRAGMENT_AT_OFFSET_SYMBOLS_AVAILABLE", True), \
                patch.object(c2pa_module, "_fragment_at_offset_probe", None), \
                patch.object(c2pa_module, "_lib") as lib:
            lib.c2pa_reader_fragment_at_offset_supported.return_value = False
            self.assertFalse(c2pa.has_fragment_at_offset())
            self.assertFalse(c2pa.has_fragment_at_offset())
            lib.c2pa_reader_fragment_at_offset_supported.assert_called_once()

    def test_unsupported_raises_before_touching_the_reader(self):
        context = Context()
        reader = Reader(DEFAULT_TEST_FILE, context=context)
        before = reader.json()
        with patch.object(
                c2pa_module, "_FRAGMENT_AT_OFFSET_SYMBOLS_AVAILABLE", False):
            with self.assertRaises(Error.NotSupported):
                reader.with_fragment_at_offset(
                    "video/mp4", io.BytesIO(b"init"), io.BytesIO(b"frag"), 0)
            with self.assertRaises(Error.NotSupported):
                Reader.from_fragment_at_offset(
                    "video/mp4", io.BytesIO(b"init"), io.BytesIO(b"frag"), 0)
        self.assertTrue(reader.is_valid)
        self.assertEqual(reader.json(), before)
        reader.close()
        context.close()


class TestFragmentAtOffsetValidation(unittest.TestCase):
    """Invalid arguments are refused before any native allocation or mutation."""

    def setUp(self):
        self.patch = patch.object(
            c2pa_module, "has_fragment_at_offset", return_value=True)
        self.patch.start()
        self.context = Context()
        self.reader = Reader(DEFAULT_TEST_FILE, context=self.context)
        self.before = self.reader.json()

    def tearDown(self):
        self.reader.close()
        self.context.close()
        self.patch.stop()

    def assert_untouched(self):
        self.assertTrue(self.reader.is_valid)
        self.assertEqual(self.reader.json(), self.before)

    def assert_invalid_inputs(self, error, format, init, fragment):
        handle = self.reader._handle
        streams = (self.reader._own_stream, self.reader._fragment_streams)
        with patch.object(c2pa_module, "_lib") as lib:
            with self.assertRaises(error):
                Reader.from_fragment_at_offset(format, init, fragment, 0)
            with self.assertRaises(error):
                self.reader.with_fragment_at_offset(format, init, fragment, 0)
            # Includes Context, Reader, and Stream allocation entry points.
            self.assertEqual(lib.mock_calls, [])
        self.assertIs(self.reader._handle, handle)
        self.assertIs(self.reader._own_stream, streams[0])
        self.assertIs(self.reader._fragment_streams, streams[1])
        self.assert_untouched()

    def test_rejects_invalid_streams_before_allocation(self):
        for invalid in (None, "init.mp4", Path("init.mp4"), b"init", object()):
            for position in (0, 1):
                with self.subTest(invalid=invalid, position=position):
                    streams = [io.BytesIO(b"i"), io.BytesIO(b"f")]
                    streams[position] = invalid
                    self.assert_invalid_inputs(TypeError, "video/mp4", *streams)

    def test_rejects_aliased_streams_before_allocation(self):
        stream = io.BytesIO(b"i")
        self.assert_invalid_inputs(ValueError, "video/mp4", stream, stream)

    def test_rejects_invalid_format_types_before_allocation(self):
        for format in (False, 0, 1, b"video/mp4", [], {}, object()):
            with self.subTest(format=format):
                self.assert_invalid_inputs(
                    TypeError, format, io.BytesIO(b"i"), io.BytesIO(b"f"))

    def test_rejects_invalid_format_encoding_before_allocation(self):
        for format in ("video/mp4\x00", "\x00video/mp4", "video/\ud800"):
            with self.subTest(format=format):
                self.assert_invalid_inputs(
                    Error, format, io.BytesIO(b"i"), io.BytesIO(b"f"))

    def test_rejects_non_int_offsets(self):
        for offset in (True, False, 1.0, "1", None, b"\x01"):
            with self.subTest(offset=offset):
                with self.assertRaises(TypeError):
                    self.reader.with_fragment_at_offset(
                        "video/mp4", io.BytesIO(b"i"), io.BytesIO(b"f"),
                        offset)
                with self.assertRaises(TypeError):
                    Reader.from_fragment_at_offset(
                        "video/mp4", io.BytesIO(b"i"), io.BytesIO(b"f"),
                        offset)
        self.assert_untouched()

    def test_rejects_out_of_range_offsets(self):
        for offset in (-1, 2**64, 2**70):
            with self.subTest(offset=offset):
                with self.assertRaises(ValueError):
                    self.reader.with_fragment_at_offset(
                        "video/mp4", io.BytesIO(b"i"), io.BytesIO(b"f"),
                        offset)
                with self.assertRaises(ValueError):
                    Reader.from_fragment_at_offset(
                        "video/mp4", io.BytesIO(b"i"), io.BytesIO(b"f"),
                        offset)
        self.assert_untouched()

    def test_from_rejects_invalid_context(self):
        with self.assertRaises(TypeError):
            Reader.from_fragment_at_offset(
                "video/mp4", io.BytesIO(b"i"), io.BytesIO(b"f"), 0,
                context=object())
        closed = Context()
        closed.close()
        with self.assertRaises(Error):
            Reader.from_fragment_at_offset(
                "video/mp4", io.BytesIO(b"i"), io.BytesIO(b"f"), 0,
                context=closed)


@unittest.skipUnless(
    c2pa.has_fragment_at_offset() or REQUIRED,
    "native library lacks c2pa_reader_with_fragment_at_offset")
class TestFragmentAtOffsetNative(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not c2pa.has_fragment_at_offset():
            raise AssertionError(
                "C2PA_FRAGMENT_OFFSET_REQUIRED=1 but the native library "
                "lacks c2pa_reader_with_fragment_at_offset")
        cls.init = _read(INIT_PATH)
        cls.fragment = _read(FRAGMENT_PATH)

    def setUp(self):
        self.context = Context()

    def tearDown(self):
        self.context.close()

    def with_fragment_codes(self):
        with Reader("video/mp4", io.BytesIO(self.init),
                    context=self.context) as reader:
            reader.with_fragment("video/mp4", io.BytesIO(self.init),
                                 io.BytesIO(self.fragment))
            return _failure_codes(reader)

    def test_zero_offset_matches_with_fragment(self):
        expected = self.with_fragment_codes()
        self.assertNotIn(BMFF_MISMATCH, expected)
        with Reader("video/mp4", io.BytesIO(self.init),
                    context=self.context) as reader:
            result = reader.with_fragment_at_offset(
                "video/mp4", io.BytesIO(self.init),
                io.BytesIO(self.fragment), 0)
            self.assertIs(result, reader)
            self.assertEqual(_failure_codes(reader), expected)

    def test_from_fragment_at_offset_zero(self):
        expected = self.with_fragment_codes()
        for context in (self.context, None):
            with self.subTest(context=context):
                with Reader.from_fragment_at_offset(
                        "video/mp4", io.BytesIO(self.init),
                        io.BytesIO(self.fragment), 0,
                        context=context) as reader:
                    self.assertEqual(_failure_codes(reader), expected)

    def test_wrong_offset_is_a_mismatch_not_an_error(self):
        for offset in (1, len(self.init), 2**32, 2**63):
            with self.subTest(offset=offset):
                with Reader.from_fragment_at_offset(
                        "video/mp4", io.BytesIO(self.init),
                        io.BytesIO(self.fragment), offset,
                        context=self.context) as reader:
                    self.assertIn(BMFF_MISMATCH, _failure_codes(reader))

    def test_maximum_offset_never_verifies(self):
        # Offset arithmetic overflows in the verifier: either an error or a
        # failure, never a clean result.
        try:
            reader = Reader.from_fragment_at_offset(
                "video/mp4", io.BytesIO(self.init),
                io.BytesIO(self.fragment), 2**64 - 1, context=self.context)
        except Error:
            return
        with reader:
            self.assertTrue(_failure_codes(reader))

    def test_replacing_the_fragment_refreshes_results(self):
        with Reader.from_fragment_at_offset(
                "video/mp4", io.BytesIO(self.init),
                io.BytesIO(self.fragment), 1,
                context=self.context) as reader:
            self.assertIn(BMFF_MISMATCH, _failure_codes(reader))
            reader.with_fragment_at_offset(
                "video/mp4", io.BytesIO(self.init),
                io.BytesIO(self.fragment), 0)
            self.assertNotIn(BMFF_MISMATCH, _failure_codes(reader))

    def test_native_failure_closes_the_reader(self):
        reader = Reader.from_fragment_at_offset(
            "video/mp4", io.BytesIO(self.init),
            io.BytesIO(self.fragment), 0, context=self.context)
        with self.assertRaises(Error):
            reader.with_fragment_at_offset(
                "video/mp4", io.BytesIO(b"\x00" * 16),
                io.BytesIO(b"\x00" * 16), 0)
        self.assertFalse(reader.is_valid)
        with self.assertRaises(Error):
            reader.json()
        reader.close()

    def test_from_fails_cleanly_without_a_manifest(self):
        with self.assertRaises(Error):
            Reader.from_fragment_at_offset(
                "video/mp4", io.BytesIO(b"\x00" * 16),
                io.BytesIO(b"\x00" * 16), 0, context=self.context)
        # The caller's context stays usable.
        self.assertTrue(self.context.is_valid)


if __name__ == "__main__":
    unittest.main()
