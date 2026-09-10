"""Tests for the bdaya-core router — reference file mapping.

Covers:
  - every router entry points to a real, loadable reference
  - load_reference rejects unknown filenames (path traversal guard)
  - list_references returns the full allowlist
  - each reference file has substantive content (not empty)
"""

from __future__ import annotations

import pytest

import doctrine


class TestRouterMapping:
    """Router entries must map to real, loadable reference files."""

    def test_list_references_non_empty(self):
        refs = doctrine.list_references()
        assert len(refs) > 0

    def test_all_references_loadable(self):
        """Every reference in the allowlist must load successfully."""
        for ref in doctrine.list_references():
            text = doctrine.load_reference(ref)
            assert text is not None, f"Reference {ref} failed to load"
            assert len(text) > 100, f"Reference {ref} is suspiciously short ({len(text)} chars)"

    def test_load_reference_rejects_unknown(self):
        """Path traversal guard: unknown filenames return None."""
        assert doctrine.load_reference("nonexistent.md") is None
        assert doctrine.load_reference("../../../etc/passwd") is None
        assert doctrine.load_reference("") is None

    def test_load_reference_rejects_non_allowlisted(self):
        """Only allowlisted references can be loaded, even if the file exists."""
        # SOUL.md exists but is NOT in the references allowlist
        assert doctrine.load_reference("SOUL.md") is None

    @pytest.mark.parametrize("ref", [
        "core-doctrine.md",
        "merge-policy.md",
        "proof-or-hedge.md",
        "memory-kb.md",
        "watchers.md",
        "mcp-economy.md",
        "git-hygiene.md",
    ])
    def test_individual_reference_loads(self, ref):
        """Each expected reference file loads and has real content."""
        text = doctrine.load_reference(ref)
        assert text is not None
        assert "# " in text  # must have at least one heading


class TestSoulLoading:
    """SOUL.md constitution loading."""

    def test_load_soul(self):
        text = doctrine.load_soul()
        assert text is not None
        assert "BCP 14" in text

    def test_soul_has_router_table(self):
        text = doctrine.load_soul()
        assert "Router" in text
        assert "references/" in text
