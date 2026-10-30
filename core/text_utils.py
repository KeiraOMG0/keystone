"""Text helpers shared by cogs (no discord / database imports)."""
from __future__ import annotations

FENCE = "```"
_CLOSE = "\n" + FENCE

# Discord limits worth naming.
MESSAGE_LIMIT = 2000
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_LIMIT = 1024


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    - Splits on line boundaries; order is preserved.
    - A single line longer than the room available is hard-split (pure slicing,
      so the pieces concatenate back to the original line).
    - A ``` code fence that is open at a chunk boundary is closed at the end of
      that chunk and re-opened (with the same language tag) at the start of the
      next, so every chunk renders correctly on its own.
    - Empty / whitespace-only input yields ``[]``.

    Use ``limit=EMBED_DESCRIPTION_LIMIT`` / ``EMBED_FIELD_LIMIT`` for embeds.
    """
    if limit < 16:
        raise ValueError("limit must be at least 16")
    if not text or not text.strip():
        return []

    chunks: list[str] = []
    cur: list[str] = []      # lines of the chunk being built (incl. re-opened header)
    cur_len = 0
    content = 0              # real lines in ``cur`` (excludes a re-opened header)
    fence: str | None = None  # header line of the currently open fence

    def reserve(fenced: bool) -> int:
        return len(_CLOSE) if fenced else 0

    def flush() -> None:
        nonlocal cur, cur_len, content
        if not content:
            return
        body = "\n".join(cur)
        if fence is not None:
            body += _CLOSE
        chunks.append(body)
        cur, cur_len, content = [], 0, 0
        if fence is not None:
            cur.append(fence)
            cur_len = len(fence)

    def add(line: str, fenced_after: bool) -> None:
        nonlocal cur_len, content
        cost = len(line) + (1 if cur else 0)
        if content and cur_len + cost + reserve(fenced_after) > limit:
            flush()
            cost = len(line) + (1 if cur else 0)
        cur.append(line)
        cur_len += cost
        content += 1

    for line in text.split("\n"):
        toggles = line.count(FENCE) % 2 == 1
        if toggles and fence is None:
            header = line.strip()
            if len(header) + 1 + len(_CLOSE) + 1 > limit:
                header = FENCE
            fence_after: str | None = header
        elif toggles:
            fence_after = None
        else:
            fence_after = fence

        # Room a single piece may use: leave space for a re-opened header and
        # the closing fence in the worst case.
        fenced_worst = fence is not None or fence_after is not None
        header_cost = (len(fence or fence_after or "") + 1) if fenced_worst else 0
        room = limit - reserve(fenced_worst) - header_cost
        if len(line) <= room:
            add(line, fence_after is not None)
        else:
            for i in range(0, len(line), room):
                add(line[i:i + room], fenced_worst)
        fence = fence_after

    if content:
        # Final chunk: any fence still open here was left open by the source text.
        chunks.append("\n".join(cur))
    return chunks
