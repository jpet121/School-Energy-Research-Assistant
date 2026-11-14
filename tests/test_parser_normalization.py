from retrofit.parser_worker import line_span


def glyph(text, left, right):
    return {"text": text, "x0": left, "x1": right, "top": 20, "bottom": 31, "size": 11, "upright": True}


def test_invisible_glyphs_do_not_expand_visible_original_geometry():
    span = line_span({"chars": [glyph("", 10, 200), glyph("A", 35, 42), glyph("B", 42, 49)]}, 1, 0, None)
    assert span["raw_text"] == "AB" and span["normalized_text"] == "AB"
    assert span["bounding_boxes"] == [[35, 20, 49, 31]]
    assert span["normalization_map"] == [0, 1]


def test_empty_or_oversize_normalized_lines_remain_ineligible():
    assert line_span({"chars": []}, 1, 0, None) is None
    assert line_span({"chars": [glyph(" ", 35, 42)]}, 1, 0, None) is None
    assert line_span({"chars": [glyph("A" * 8193, 35, 42)]}, 1, 0, None) is None
    assert line_span({"chars": [glyph("A" * 8191, 35, 42), glyph("B", 60, 67)]}, 1, 0, None) is None
