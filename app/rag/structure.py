"""Markdown block boundaries and bounded slices in original Unicode coordinates."""
import re

from markdown_it import MarkdownIt


def markdown_regions(text):
    lines = [0, *(m.end() for m in re.finditer(r'\r\n?|\n', text)), len(text)]
    tokens = MarkdownIt('commonmark').enable('table').parse(text)
    title, cursor = '', 0
    for i, token in enumerate(tokens):
        if token.type not in ('heading_open', 'table_open', 'fence', 'code_block'):
            continue
        start, end = (lines[line] for line in token.map)
        if start < cursor:
            continue
        if start > cursor:
            yield cursor, start, title, 'text'
        if token.type == 'heading_open':
            title = ('#' * int(token.tag[1:]) + ' ' + tokens[i + 1].content)[:256]
            cursor = start
        else:
            yield start, end, title, 'table' if token.type == 'table_open' else 'code'
            cursor = end
    if cursor < len(text):
        yield cursor, len(text), title, 'text'


def region_spans(text, start, end, kind, size, overlap):
    if not 0 <= overlap < size:
        raise ValueError('Invalid chunk size or overlap')
    structured = kind in ('code', 'table')
    while start < end:
        stop = min(start + size, end)
        if stop < end:
            # Keep a whole row or code line when it fits. A line longer than
            # the limit uses consecutive slices of the same original block.
            floor = start if structured else start + size // 2
            for separator in (('\n', '\r') if structured else ('\n\n', '\n', '。', '. ', ' ')):
                boundary = text.rfind(separator, floor, stop)
                if boundary >= floor:
                    stop = boundary + len(separator)
                    break
        left, right = start, stop
        if not structured:
            while left < right and text[left].isspace():
                left += 1
            while right > left and text[right - 1].isspace():
                right -= 1
        if right > left:
            yield left, right
        if stop == end:
            break
        start = stop if structured else max(start + 1, stop - overlap)
