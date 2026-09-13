"""Text helpers for splitting by rune count, ported from legacy/go/internal/lark/message.go."""

MAX_CARD_RUNES = 6000


def chunk_runes(text: str, max_run: int = MAX_CARD_RUNES):
    """Split by rune count, preferring newline breaks in the latter half."""
    if len(text) <= max_run:
        return [text]
    chunks = []
    s = text
    while s:
        if len(s) <= max_run:
            chunks.append(s)
            break
        cut = max_run
        head = s[:max_run]
        nl = head.rfind("\n")
        if nl > max_run // 2:
            cut = nl + 1
        chunks.append(s[:cut])
        s = s[cut:]
    return chunks
