"""Tests for stale-doctrine drift detection via content checksums.

The doctrine checksum system detects when the bundled reference files
diverge from the source of truth (e.g., after an upstream update that
wasn't propagated to the plugin).

Covers:
  - checksums dict has entries for SOUL.md and every reference
  - checksums are stable (same content → same hash)
  - modified content → different checksum (drift detected)
  - missing files are absent from the checksum dict
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest import mock

import pytest

import doctrine


class TestDriftDetection:
    """Stale-doctrine drift is detectable via content checksums."""

    def test_checksums_includes_soul(self):
        checksums = doctrine.compute_checksums()
        assert "SOUL.md" in checksums

    def test_checksums_includes_all_references(self):
        checksums = doctrine.compute_checksums()
        for ref in doctrine.list_references():
            key = f"references/{ref}"
            assert key in checksums, f"Missing checksum for {key}"

    def test_checksums_are_sha256(self):
        """Each checksum must be a 64-character hex string (SHA-256)."""
        checksums = doctrine.compute_checksums()
        for path, digest in checksums.items():
            assert len(digest) == 64, f"Checksum for {path} is not SHA-256 length"
            assert all(c in "0123456789abcdef" for c in digest), f"Checksum for {path} is not hex"

    def test_checksums_are_stable(self):
        """Running compute_checksums twice returns the same values."""
        first = doctrine.compute_checksums()
        second = doctrine.compute_checksums()
        assert first == second

    def test_checksum_changes_on_content_change(self):
        """Modified content produces a different checksum — drift detected."""
        original = doctrine.compute_checksums()
        original_soul = original["SOUL.md"]

        # Simulate content change by mocking load_soul
        with mock.patch.object(doctrine, "load_soul", return_value="MODIFIED CONTENT"):
            modified = doctrine.compute_checksums()

        assert modified["SOUL.md"] != original_soul
        expected = hashlib.sha256(b"MODIFIED CONTENT").hexdigest()
        assert modified["SOUL.md"] == expected

    def test_missing_file_absent_from_checksums(self):
        """A missing file is absent from the checksum dict (not an error)."""
        with mock.patch.object(doctrine, "load_soul", return_value=None):
            checksums = doctrine.compute_checksums()
        assert "SOUL.md" not in checksums

    def test_drift_comparison_workflow(self):
        """Simulate the full drift-detection workflow:
        1. Record baseline checksums
        2. Detect no drift when content unchanged
        3. Detect drift when content changes
        """
        # Step 1: baseline
        baseline = doctrine.compute_checksums()

        # Step 2: no drift
        current = doctrine.compute_checksums()
        drifted_files = [
            path for path in baseline
            if path in current and current[path] != baseline[path]
        ]
        assert drifted_files == []

        # Step 3: drift on one file
        with mock.patch.object(doctrine, "load_soul", return_value="DRIFTED"):
            drifted = doctrine.compute_checksums()
        drifted_files = [
            path for path in baseline
            if path in drifted and drifted[path] != baseline[path]
        ]
        assert drifted_files == ["SOUL.md"]
