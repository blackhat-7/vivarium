import json
import unittest
from dataclasses import FrozenInstanceError

from external_gate.diff_viewer import (
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    MAX_FILES,
    FilePatch,
    OmittedFilePatch,
    PreviewSelection,
    build_preview,
    load_preview,
    render_preview,
)


def patch(path, old_lines, new_lines, *, context=""):
    old_count = len(old_lines)
    new_count = len(new_lines)
    body = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{old_count} +1,{new_count} @@",
    ]
    body.extend(f"-{line}" for line in old_lines)
    body.extend(f"+{line}" for line in new_lines)
    if context:
        body.append(f" {context}")
    return "\n".join(body) + "\n"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class DiffViewerTests(unittest.TestCase):
    def test_canonical_artifact_is_deterministic_and_immutable(self):
        files = [
            FilePatch(
                "src/a.py",
                patch("src/a.py", ["old"], ["new"]),
                metadata={"language": "python"},
            )
        ]
        first = build_preview(files, metadata={"target": "main"})
        second = build_preview(iter(files), metadata={"target": "main"})
        self.assertEqual(first, second)
        self.assertNotIn(b": ", first)
        self.assertNotIn(b", ", first)
        preview = load_preview(first)
        self.assertIsInstance(preview.files, tuple)
        self.assertIsInstance(preview.files[0].metadata, tuple)
        with self.assertRaises(FrozenInstanceError):
            preview.files[0].path = "changed"

    def test_hostile_html_invalid_utf8_controls_and_bidi_are_neutralized(self):
        hostile_path = '<img src=x onerror="alert(1)">\u202e.py'
        hostile_patch = (
            b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
            b"-safe\n+<script>alert(1)</script>\xe2\x80\xae\xff\x00&\n"
        )
        artifact = build_preview([FilePatch(hostile_path, hostile_patch)])
        preview = load_preview(artifact)
        self.assertNotIn("\u202e", preview.files[0].path)
        self.assertIn("\\u202E", preview.files[0].path)
        self.assertIn("�", preview.files[0].patch)
        self.assertNotIn("\x00", preview.files[0].patch)

        rendered = render_preview(preview, html_budget=8_000)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("&amp;", rendered)

    def test_aligned_word_and_punctuation_highlights(self):
        artifact = build_preview(
            [FilePatch("message.txt", patch("message.txt", ["hello, old world!"], ["hello, brave world?"]))]
        )
        rendered = render_preview(artifact, html_budget=8_000)
        self.assertIn('<tr class="changed">', rendered)
        self.assertIn('<mark class="intra-delete">old</mark>', rendered)
        self.assertIn('<mark class="intra-delete">!</mark>', rendered)
        self.assertIn('<mark class="intra-add">brave</mark>', rendered)
        self.assertIn('<mark class="intra-add">?</mark>', rendered)
        self.assertIn('<td class="old-number">1</td>', rendered)
        self.assertIn('<td class="new-number">1</td>', rendered)

    def test_too_large_file_does_not_hide_later_small_file(self):
        huge = (
            "diff --git a/huge b/huge\n--- a/huge\n+++ b/huge\n@@ -0,0 +1 @@\n+"
            + ("x" * MAX_FILE_BYTES)
            + "\n"
        )
        artifact = build_preview(
            [
                FilePatch("huge", huge),
                FilePatch("small", patch("small", ["before"], ["visible-after-large"])),
            ]
        )
        preview = load_preview(artifact)
        self.assertEqual(preview.files[0].omission_reason, "per_file_bytes")
        self.assertIsNone(preview.files[0].patch)
        self.assertIsNone(preview.files[1].omission_reason)
        self.assertIn("visible-after-large", preview.files[1].patch)
        rendered = render_preview(preview, file="small", html_budget=8_000)
        self.assertIn("visible-after-large", rendered)

    def test_aggregate_cap_omits_only_non_fitting_content_and_keeps_summaries(self):
        def large_file(name, marker):
            payload = marker + ("x" * (400 * 1024))
            return FilePatch(name, patch(name, [], [payload]))

        artifact = build_preview(
            [
                large_file("one", "ONE"),
                large_file("two", "TWO"),
                large_file("three", "THREE"),
                FilePatch("later-small", patch("later-small", [], ["still-visible"])),
            ]
        )
        preview = load_preview(artifact)
        self.assertIsNotNone(preview.files[0].patch)
        self.assertIsNotNone(preview.files[1].patch)
        self.assertEqual(preview.files[2].omission_reason, "aggregate_bytes")
        self.assertIsNone(preview.files[2].patch)
        self.assertGreater(preview.files[2].additions, 0)
        self.assertIsNotNone(preview.files[3].patch)
        self.assertIn("still-visible", preview.files[3].patch)
        omitted = render_preview(preview, file="three", html_budget=4_000)
        self.assertIn('data-reason="aggregate_bytes"', omitted)
        self.assertIn("aggregate 1 MiB limit exceeded", omitted)

    def test_three_hundred_file_limit_and_extra_count(self):
        files = (FilePatch(f"file-{number}.txt", "") for number in range(MAX_FILES))
        preview = load_preview(build_preview(files, extra_file_count=7))
        self.assertEqual(len(preview.files), 300)
        self.assertEqual(preview.extra_file_count, 7)
        self.assertEqual(preview.files[-1].path, "file-299.txt")
        rendered = render_preview(preview, file=299, html_budget=5_000)
        self.assertIn("7 additional file(s) not listed", rendered)
        with self.assertRaises(ValueError):
            build_preview([FilePatch(str(number), "") for number in range(MAX_FILES)] + [object()])

    def test_line_limits_omit_only_the_non_fitting_file(self):
        too_many = FilePatch(
            "too-many.txt",
            patch("too-many.txt", [], ["x"] * (MAX_FILE_LINES + 1)),
        )
        preview = load_preview(
            build_preview(
                [too_many, FilePatch("after.txt", patch("after.txt", [], ["visible-after-lines"]))]
            )
        )
        self.assertEqual(preview.files[0].omission_reason, "per_file_lines")
        self.assertIn("visible-after-lines", preview.files[1].patch)

        aggregate = load_preview(
            build_preview(
                [
                    FilePatch("one", patch("one", [], ["x"] * 9_000)),
                    FilePatch("two", patch("two", [], ["x"] * 9_000)),
                    FilePatch("three", patch("three", [], ["x"] * 5_000)),
                    FilePatch("later", patch("later", [], ["still-visible"])),
                ]
            )
        )
        self.assertEqual(aggregate.files[2].omission_reason, "aggregate_lines")
        self.assertIn("still-visible", aggregate.files[3].patch)

    def test_streamed_omission_summary_is_strict_and_does_not_retain_content(self):
        artifact = build_preview(
            [
                OmittedFilePatch(
                    "huge.txt",
                    "per_file_bytes",
                    MAX_FILE_BYTES + 1,
                    1,
                ),
                FilePatch("later.txt", patch("later.txt", [], ["visible"])),
            ]
        )
        preview = load_preview(artifact)
        self.assertEqual(preview.files[0].omission_reason, "per_file_bytes")
        self.assertIsNone(preview.files[0].patch)
        self.assertIn("visible", preview.files[1].patch)
        with self.assertRaises(ValueError):
            build_preview(
                [OmittedFilePatch("bad.txt", "per_file_bytes", MAX_FILE_BYTES, 1)]
            )

    def test_strict_loader_rejects_invalid_or_noncanonical_artifacts(self):
        valid = build_preview([FilePatch("a", patch("a", [], ["x"]))])
        value = json.loads(valid)

        with self.assertRaises(ValueError):
            load_preview(json.dumps(value, indent=2).encode())
        with self.assertRaises(ValueError):
            load_preview(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            load_preview(b"\xff")
        lone_surrogate = valid.replace(b'"path":"a"', b'"path":"\\ud800"')
        with self.assertRaises(ValueError):
            load_preview(lone_surrogate)
        with self.assertRaises(ValueError):
            render_preview(lone_surrogate)

        value["files"][0]["line_count"] += 1
        with self.assertRaises(ValueError):
            load_preview(canonical(value))

        value = json.loads(valid)
        value["unexpected"] = True
        with self.assertRaises(ValueError):
            load_preview(canonical(value))

    def test_query_file_selection_and_per_file_pagination(self):
        many_old = [f"old-{number}-" + ("o" * 30) for number in range(30)]
        many_new = [f"new-{number}-" + ("n" * 30) for number in range(30)]
        preview = load_preview(
            build_preview(
                [
                    FilePatch("first.txt", patch("first.txt", ["first"], ["only"])),
                    FilePatch("folder/second file.txt", patch("folder/second file.txt", many_old, many_new)),
                ]
            )
        )
        first_page = render_preview(
            preview,
            query="file=folder%2Fsecond+file.txt&page=1",
            html_budget=2_600,
        )
        second_page = render_preview(
            preview,
            query={"file": ["folder/second file.txt"], "page": ["2"]},
            html_budget=2_600,
        )
        self.assertIn("folder/second file.txt", first_page)
        self.assertIn("Page 1 of", first_page)
        self.assertIn("Page 2 of", second_page)
        self.assertNotEqual(first_page, second_page)
        self.assertIn("previous-page", second_page)

        selected = render_preview(preview, PreviewSelection(file_index=0, page=1), html_budget=3_000)
        self.assertIn("first.txt", selected)
        for invalid_query in (
            "file",
            "file=missing&page=1",
            "file=1&file_index=1&page=1",
            "file_index=0&page=1",
            "file=1&page=0",
            "other=1",
        ):
            with self.subTest(invalid_query=invalid_query), self.assertRaises(ValueError):
                render_preview(preview, query=invalid_query, html_budget=3_000)

    def test_generated_navigation_is_unambiguous_for_numeric_paths(self):
        preview = load_preview(
            build_preview(
                [
                    FilePatch("1", patch("1", [], ["first"])),
                    FilePatch("alpha", patch("alpha", [], ["second"])),
                    FilePatch("2", patch("2", [], ["third"])),
                ]
            )
        )
        first = render_preview(preview, file="1", html_budget=5_000)
        self.assertIn("?file_index=2&amp;page=1", first)
        second = render_preview(preview, query="file_index=2&page=1", html_budget=5_000)
        self.assertIn("<h3>alpha</h3>", second)
        self.assertNotIn("<h3>2</h3>", second)

    def test_rendered_html_never_exceeds_budget(self):
        very_long_lines = ["<&>" * 300 for _ in range(20)]
        artifact = build_preview([FilePatch("escape-heavy", patch("escape-heavy", [], very_long_lines))])
        for budget in (0, 40, 500, 1_500, 3_000):
            with self.subTest(budget=budget):
                rendered = render_preview(artifact, html_budget=budget)
                self.assertLessEqual(len(rendered.encode()), budget)
        rendered = render_preview(artifact, html_budget=3_000)
        self.assertIn("trusted-diff-preview", rendered)

    def test_binary_created_deleted_and_no_newline_markers(self):
        binary = FilePatch(
            "image.png",
            "diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n",
        )
        created = FilePatch(
            "new.txt",
            "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n\\ No newline at end of file\n",
        )
        deleted = FilePatch(
            "gone.txt",
            "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n",
        )
        preview = load_preview(build_preview([binary, created, deleted]))
        self.assertTrue(preview.files[0].binary)
        self.assertEqual(preview.files[0].omission_reason, "binary")
        self.assertEqual(preview.files[1].status, "created")
        self.assertEqual(preview.files[2].status, "deleted")
        created_html = render_preview(preview, file="new.txt", html_budget=6_000)
        self.assertIn("No newline at end of file", created_html)
        self.assertIn('<td class="old-number"></td>', created_html)
        self.assertIn('<td class="new-number">1</td>', created_html)
        binary_html = render_preview(preview, file="image.png", html_budget=3_000)
        self.assertIn("Binary file content is not shown", binary_html)


if __name__ == "__main__":
    unittest.main()
