"""Git history regression tests using synthetic repositories and an in-memory store.

Run without services or third-party packages: python -m unittest nodes.test_gitimport -v
The store checks import data flow; it does not execute FalkorDB Cypher.
"""
import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nodes import gitimport


class MemoryStore:
    """Only the Redis and named-query surface used by import_git, with strict reads."""

    ws = "synthetic"

    def __init__(self, paths=()):
        self.r = self
        self.meta = None
        self.paths = set(paths)
        self.revisions = {}
        self.calls = []

    def hget(self, key, field):
        assert (key, field) == ("nodes:git", self.ws)
        return self.meta

    def hset(self, key, field, value):
        assert (key, field) == ("nodes:git", self.ws)
        self.meta = value.encode()

    def q(self, name, query, params=None, ro=False):
        self.calls.append((name, copy.deepcopy(params)))
        if name == "git.commits":
            self.revisions.update((c["sha"], copy.deepcopy(c)) for c in params["rows"])
        elif name == "git.history":
            return [[sorted(self.paths.intersection(c["files"]))] for c in self.revisions.values()
                    if c.get("files_changed", len(c["files"])) <= params["max"]
                    and self.paths.intersection(c["files"])]
        elif name == "git.msgs":
            return [[c["sha"], c["message"], c["pr"]] for c in self.revisions.values()]
        elif ro:
            raise AssertionError(f"Unexpected read: {name}")
        return []

    def rows(self, name):
        return [row for called, params in self.calls if called == name for row in params["rows"]]


class GitImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nodes17-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Ignore the machine's identities, hooks, signing, and Git configuration.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_AUTHOR_DATE": "2026-01-01T12:00:00+00:00",
                         "GIT_COMMITTER_DATE": "2026-01-01T12:00:00+00:00"})
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.run_git("init", "-q", "-b", "main")

    def run_git(self, *args, data=None):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Synthetic Developer",
                               "-c", "user.email=SYNTHETIC@example.invalid", "-c", "core.hooksPath=/dev/null",
                               "-c", "commit.gpgsign=false", *args], input=data, env=self.env,
                              capture_output=True, check=True).stdout.decode("utf-8")

    def write(self, path, content="value = 1\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message, paths=()):
        for path in paths:
            self.write(path)
        self.run_git("add", "-A")
        self.run_git("commit", "--allow-empty", "-q", "--cleanup=verbatim", "-F", "-",
                     data=message.encode("utf-8"))
        return self.run_git("rev-parse", "HEAD").strip()

    def test_exact_unicode_and_whitespace_paths(self):
        paths = ["café/ü.py", " leading.py", "trailing.py ", "tab\tname.py", "line\nname.py",
                 "\nfirst.py", 'quote"slash\\.py', "carriage\rname.py", "control\x1e\x1f.py"]
        sha = self.commit("Preserve every changed path (#17)\n", paths)
        rows = gitimport.commits(self.root, None)
        self.assertEqual([c["sha"] for c in rows], [sha])
        self.assertCountEqual(rows[0]["files"], paths)
        self.assertEqual(rows[0]["pr"], 17)

    def test_subdirectory_paths_and_outside_commits(self):
        first = self.commit("Add files inside and outside service\n", ["svc/a.py", "svc/b.py", "outside.py"])
        outside = self.commit("Record an unrelated outside change\n", ["other.py"])
        rows = gitimport.commits(self.root / "svc", None)
        self.assertEqual([c["sha"] for c in rows], [outside, first])
        self.assertEqual(rows[0]["files"], [])
        self.assertCountEqual(rows[1]["files"], ["a.py", "b.py"])

    def test_subdirectory_prefix_preserves_unicode_newlines_and_boundaries(self):
        prefix = " café\n"
        self.commit("Keep unusual directory prefixes exact\n", [prefix + "/ü.py", prefix + "-other/outside.py"])
        row, = gitimport.commits(self.root / prefix, None)
        self.assertEqual(row["files"], ["ü.py"])
        self.assertEqual(row["files_changed"], 2)

    def assert_repository_wide_cochange_guard(self, outside_count):
        paths = ["svc/a.py", "svc/b.py"] + [f"outside-{i}.txt" for i in range(outside_count)]
        self.commit("Add service files and repository-wide changes\n", paths)
        for path in paths:
            self.write(path, "value = 2\n")
        self.commit("Update service files and repository-wide changes\n")
        self.run_git("config", "diff.relative", "true")  # Explicit raw log paths must override this local preference.
        for subtree in (False, True):
            with self.subTest(subtree=subtree, total_files=len(paths)):
                root = self.root / "svc" if subtree else self.root
                indexed = ["a.py", "b.py"] if subtree else ["svc/a.py", "svc/b.py"]
                store = MemoryStore(indexed)
                stats = gitimport.import_git(store, root, [])
                expected_edges = int(len(paths) <= gitimport.MAX_FILES_PER_COMMIT)
                self.assertEqual(stats["cochange_edges"], expected_edges)
                rows = store.rows("git.commits")
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(row["files_changed"], len(paths))
                    self.assertCountEqual(row["files"], indexed if subtree else paths)
                if expected_edges:
                    self.assertEqual(store.rows("git.cochange"),
                                     [{"a": indexed[0], "b": indexed[1], "s": 2, "c": 1.0}])
                else:
                    self.assertEqual(store.rows("git.cochange"), [])
                # Recomputing from stored history must preserve the same repository-wide guard.
                again = gitimport.import_git(store, root, [])
                self.assertEqual(again["new_commits"], 0)
                self.assertEqual(again["cochange_edges"], expected_edges)

    def test_cochange_excludes_33_file_commits_at_root_and_subtree(self):
        self.assert_repository_wide_cochange_guard(outside_count=31)

    def test_cochange_excludes_31_file_commits_at_root_and_subtree(self):
        self.assert_repository_wide_cochange_guard(outside_count=29)

    def test_cochange_includes_30_file_commits_at_root_and_subtree(self):
        self.assert_repository_wide_cochange_guard(outside_count=28)

    def test_cochange_includes_small_commits_at_root_and_subtree(self):
        self.assert_repository_wide_cochange_guard(outside_count=0)

    def test_control_messages_preserve_neighbors_and_fields(self):
        oldest = self.commit("Add the first normal neighbor\n", ["old.py"])
        message = "Preserve record \x1e and field \x1f controls (#42)\n\nBody\nwith\rreturns\x1e\x1f\n"
        middle = self.commit(message, ["middle.py"])
        newest = self.commit("Add the last normal neighbor\n", ["new.py"])
        rows = gitimport.commits(self.root, None)
        self.assertEqual([c["sha"] for c in rows], [newest, middle, oldest])
        self.assertEqual(rows[1]["message"], message.strip())
        self.assertEqual(rows[1]["author"], "Synthetic Developer")
        self.assertEqual(rows[1]["email"], "synthetic@example.invalid")
        self.assertEqual(rows[1]["at"], 1767268800)
        self.assertEqual(rows[1]["parents"], [oldest])
        self.assertEqual(rows[1]["pr"], 42)
        self.assertEqual([c["files"] for c in rows], [["new.py"], ["middle.py"], ["old.py"]])

    def test_field_separator_alone_does_not_move_message_into_paths(self):
        message = "Keep field \x1f separators inside the message (#7)\n"
        self.commit(message, ["a.py"])
        row, = gitimport.commits(self.root, None)
        self.assertEqual(row["message"], message.strip())
        self.assertEqual(row["files"], ["a.py"])
        self.assertEqual(row["pr"], 7)

    def test_empty_messages_empty_commits_and_incremental_since(self):
        first = self.commit("Add initial normal files\n", ["a.py"])
        self.run_git("commit", "--allow-empty", "--allow-empty-message", "-q", "-m", "")
        empty = self.run_git("rev-parse", "HEAD").strip()
        last = self.commit("Keep the last normal commit\n", ["b.py"])
        rows = gitimport.commits(self.root, first)
        self.assertEqual([c["sha"] for c in rows], [last, empty])
        self.assertEqual(rows[1]["message"], "")
        self.assertEqual(rows[1]["files"], [])
        self.assertEqual(gitimport.commits(self.root, last), [])

    def test_import_attribution_cochange_facts_blame_and_checkpoint(self):
        paths = ["café/ü.py", "line\nname.py"]
        first = self.commit("Add unicode service paths for indexing\n", ["svc/" + p for p in paths])
        for p in paths:
            self.write("svc/" + p, "value = 2\n")
        message = "Retain changed paths \x1e with field \x1f controls (#23)\n"
        last = self.commit(message)
        units = [{"uid": "file:" + p, "kind": "file", "path": p} for p in paths]
        units += [{"uid": "region:" + p, "kind": "region", "path": p, "start": 1, "end": 1} for p in paths]
        store = MemoryStore(paths)
        stats = gitimport.import_git(store, self.root / "svc", units, batch=1)
        self.assertEqual(stats["new_commits"], 2)
        self.assertEqual(stats["commit_facts"], 2)
        self.assertEqual(stats["cochange_edges"], 1)
        self.assertEqual(stats["owned_units"], 2)
        self.assertEqual(set(store.revisions), {first, last})
        for c in store.revisions.values():
            self.assertCountEqual(c["files"], paths)
        self.assertEqual(store.rows("git.cochange"), [{"a": paths[0], "b": paths[1], "s": 2, "c": 1.0}])
        self.assertCountEqual([r["sha"] for r in store.rows("git.facts")], [first, last])
        for fact in store.rows("git.facts"):
            self.assertCountEqual(fact["files"], paths)
        self.assertEqual({r["sha"] for r in store.rows("git.rationale")}, {last})
        self.assertTrue(all(r["email"] == "synthetic@example.invalid" for r in store.rows("git.owners")))
        self.assertEqual(store.meta, last.encode())
        store.calls.clear()
        again = gitimport.import_git(store, self.root / "svc", units)
        self.assertEqual(again["new_commits"], 0)
        self.assertEqual(again["cochange_edges"], 1)
        self.assertEqual(again["commit_facts"], 0)
        self.assertEqual(store.rows("git.commits"), [])
        self.assertEqual({r["sha"] for r in store.rows("git.rationale")}, {last})
        for p in paths:
            self.write("svc/" + p, "value = 3\n")
        newest = self.commit("Keep incremental attribution exact\n")
        store.calls.clear()
        incremental = gitimport.import_git(store, self.root / "svc", units)
        self.assertEqual(incremental["new_commits"], 1)
        self.assertEqual(incremental["commit_facts"], 1)
        self.assertEqual([c["sha"] for c in store.rows("git.commits")], [newest])
        self.assertEqual(store.rows("git.cochange")[0]["s"], 3)
        self.assertCountEqual(store.rows("git.facts")[0]["files"], paths)
        self.assertEqual(store.meta, newest.encode())

    def test_normal_merge_rename_delete_and_rewritten_checkpoint(self):
        first = self.commit("Create the ordinary starting files\n", ["a.py", "b.py"])
        self.run_git("checkout", "-qb", "feature")
        feature = self.commit("Add the ordinary feature file\n", ["feature.py"])
        self.run_git("checkout", "-q", "main")
        main = self.commit("Add an independent main file\n", ["main.py"])
        self.run_git("merge", "--no-ff", "-qm", "Merge pull request #12 from feature", "feature")
        merge = self.run_git("rev-parse", "HEAD").strip()
        self.run_git("mv", "a.py", "renamed.py")
        self.run_git("rm", "-q", "b.py")
        newest = self.commit("Rename and delete ordinary files\n")
        rows = gitimport.commits(self.root, None)
        self.assertEqual({c["sha"] for c in rows}, {first, feature, main, merge, newest})
        by_sha = {c["sha"]: c for c in rows}
        self.assertEqual(by_sha[merge]["parents"], [main, feature])
        self.assertEqual(by_sha[merge]["pr"], 12)
        self.assertEqual(by_sha[merge]["files"], [])
        self.assertCountEqual(by_sha[newest]["files"], ["b.py", "renamed.py"])
        store = MemoryStore(["renamed.py", "main.py", "feature.py"])
        store.meta = ("0" * 40).encode()  # A checkpoint whose commit no longer exists.
        stats = gitimport.import_git(store, self.root, [])
        self.assertEqual(stats["new_commits"], 5)
        self.assertEqual(stats["merges"], 1)
        self.assertEqual(stats["prs"], 1)
        self.assertEqual(store.meta, newest.encode())

    def test_unborn_repo_is_distinct_from_non_repository(self):
        self.write("untracked.py")
        store = MemoryStore()
        self.assertEqual(gitimport.import_git(store, self.root, []), {"git": "no commits"})
        self.assertEqual(store.calls, [])
        self.assertIsNone(store.meta)
        outside = self.root / "not-a-repo"
        outside.mkdir()
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            self.assertEqual(gitimport.import_git(store, outside, []), {"git": "not a git repository"})

    def test_malformed_stream_is_reported_before_import_writes(self):
        sha = self.commit("Add a normal commit for validation\n", ["a.py"])
        stream = "\0".join(["", sha, "Synthetic", "synthetic@example.invalid", "1767268800", "",
                             "A valid message", "\na.py", ""])
        # Missing terminators, missing fields, invalid timestamps, and invalid file framing must fail loudly.
        for malformed in ["not a framed stream", f"\0{sha}\0Synthetic\0", stream[:-1],
                          stream.replace("1767268800", "invalid"), stream.replace("\na.py", "a.py"),
                          stream.replace(sha, "invalid-sha"), stream + "\0broken\0"]:
            with self.subTest(stream=repr(malformed)):
                real_git = gitimport.git
                def fake_git(root, *args):
                    return malformed if "log" in args else real_git(root, *args)
                store = MemoryStore(["a.py"])
                with patch.object(gitimport, "git", side_effect=fake_git):
                    with self.assertRaises(ValueError):
                        gitimport.import_git(store, self.root, [])
                self.assertEqual(store.calls, [])
                self.assertIsNone(store.meta)


if __name__ == "__main__":
    unittest.main()
