"""core/text_utils.split_message: line splitting, fences, long lines, limits."""
from __future__ import annotations

import pytest

from core.text_utils import split_message


def test_empty_and_blank_input():
    assert split_message("") == []
    assert split_message("  \n \n") == []


def test_short_text_is_one_chunk_unchanged():
    assert split_message("hello\nworld") == ["hello\nworld"]


def test_splits_on_line_boundaries_and_preserves_order():
    lines = [f"line {i:03d}" for i in range(100)]
    chunks = split_message("\n".join(lines), limit=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines  # nothing lost, nothing reordered, no mid-line cuts


def test_exact_limit_boundaries():
    assert split_message("a" * 2000) == ["a" * 2000]
    two = split_message("a" * 2000 + "\n" + "b" * 2000)
    assert two == ["a" * 2000, "b" * 2000]
    # two lines that fit exactly (including the joining newline) stay together
    assert split_message("a" * 999 + "\n" + "b" * 1000) == ["a" * 999 + "\n" + "b" * 1000]
    # one char over must split
    assert len(split_message("a" * 1000 + "\n" + "b" * 1000)) == 2


def test_over_long_single_line_is_hard_split_and_reassembles():
    line = "x" * 5000
    chunks = split_message(line, limit=2000)
    assert [len(c) for c in chunks] == [2000, 2000, 1000]
    assert "".join(chunks) == line


def test_embed_limits():
    text = "\n".join("y" * 50 for _ in range(200))
    for limit in (4096, 1024):
        chunks = split_message(text, limit=limit)
        assert all(len(c) <= limit for c in chunks)
        assert "\n".join(chunks) == text


def test_fence_is_closed_and_reopened_across_chunks():
    body = "\n".join(f"row {i:02d}" for i in range(40))
    text = f"intro\n```py\n{body}\n```\noutro"
    chunks = split_message(text, limit=120)
    assert len(chunks) > 2
    for c in chunks:
        assert len(c) <= 120
        assert c.count("```") % 2 == 0, c
    assert chunks[0].startswith("intro\n```py")
    assert chunks[1].startswith("```py")  # re-opened with the language tag
    assert chunks[-1].endswith("outro")
    # the real content is intact and ordered
    rows = [ln for c in chunks for ln in c.split("\n") if ln.startswith("row ")]
    assert rows == [f"row {i:02d}" for i in range(40)]


def test_long_line_inside_fence_keeps_every_chunk_balanced():
    url = "https://example.com/" + "z" * 500
    chunks = split_message(f"```\n{url}\n```", limit=100)
    assert all(len(c) <= 100 and c.count("```") % 2 == 0 for c in chunks)
    rebuilt = "".join(ln for c in chunks for ln in c.split("\n") if ln not in ("```",))
    assert rebuilt == url


def test_inline_triple_backticks_on_one_line_do_not_open_a_fence():
    text = "a ```code``` b\n" + "\n".join("z" * 40 for _ in range(10))
    chunks = split_message(text, limit=100)
    assert all(c.count("```") == (2 if i == 0 else 0) for i, c in enumerate(chunks))


def test_unclosed_fence_in_source_is_left_alone_at_the_end():
    chunks = split_message("```\nabc", limit=100)
    assert chunks == ["```\nabc"]


def test_tiny_limit_rejected():
    with pytest.raises(ValueError):
        split_message("abc", limit=5)
