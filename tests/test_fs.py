"""Jail tests for host filesystem paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_machine.fs import (
    FsError,
    HOME_MOUNT,
    JailedFilesystemBackend,
    host_path_from_virtual,
    resolve_user_path,
)


class ResolveUserPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "Documents").mkdir()
        (self.root / ".ssh").mkdir()
        (self.root / "Documents" / "note.txt").write_text("hi", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relative_stays_inside_root(self) -> None:
        path = resolve_user_path("Documents/note.txt", root=self.root)
        self.assertEqual(path, self.root / "Documents" / "note.txt")

    def test_rejects_parent_escape(self) -> None:
        with self.assertRaises(FsError):
            resolve_user_path("../outside", root=self.root)

    def test_rejects_ssh(self) -> None:
        with self.assertRaises(FsError):
            resolve_user_path(".ssh/id_rsa", root=self.root)

    def test_rejects_env_name(self) -> None:
        with self.assertRaises(FsError):
            resolve_user_path("Documents/.env", root=self.root)

    def test_symlink_escape(self) -> None:
        outside = Path(self._tmp.name).parent / "outside-secret"
        try:
            outside.write_text("nope", encoding="utf-8")
            link = self.root / "Documents" / "trap"
            link.symlink_to(outside)
            with self.assertRaises(FsError):
                resolve_user_path("Documents/trap", root=self.root)
        finally:
            outside.unlink(missing_ok=True)

    def test_extra_deny_prefix(self) -> None:
        with self.assertRaises(FsError):
            resolve_user_path(
                "Documents/note.txt",
                root=self.root,
                extra_deny=("Documents",),
            )


class JailedFilesystemBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "Documents").mkdir()
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_ed25519").write_text("secret", encoding="utf-8")
        (self.root / "Documents" / "note.txt").write_text("hello", encoding="utf-8")
        self.backend = JailedFilesystemBackend(root_dir=self.root, virtual_mode=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_home_files(self) -> None:
        result = self.backend.read("/Documents/note.txt")
        self.assertIsNone(result.error)
        payload = result.file_data
        text = payload.get("content") if isinstance(payload, dict) else getattr(payload, "content", "")
        self.assertIn("hello", str(text))

    def test_hides_ssh_from_listing(self) -> None:
        listing = self.backend.ls("/")
        names = [entry["path"] for entry in listing.entries or []]
        self.assertTrue(any("Documents" in name for name in names))
        self.assertFalse(any(".ssh" in name for name in names))

    def test_blocks_ssh_read(self) -> None:
        with self.assertRaises(PermissionError):
            self.backend._resolve_path("/.ssh/id_ed25519")

    def test_virtual_home_maps_to_jail(self) -> None:
        path = host_path_from_virtual(
            f"{HOME_MOUNT}/Documents/note.txt",
            root=self.root,
        )
        self.assertEqual(path, self.root / "Documents" / "note.txt")


if __name__ == "__main__":
    unittest.main()
