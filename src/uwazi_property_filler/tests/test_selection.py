from uwazi_property_filler.domain.selection import TextItem, selection_text, word_range_to_rectangles


def _word(page: int, text: str, left: float, top: float, width: float = 20.0, height: float = 10.0) -> TextItem:
    return TextItem(page=page, text=text, left=left, top=top, width=width, height=height)


def test_single_line_yields_one_rectangle() -> None:
    items = [
        _word(1, "one", 10.0, 100.0),
        _word(1, "two", 35.0, 100.0),
        _word(1, "three", 60.0, 100.0),
    ]
    rects = word_range_to_rectangles(items, 0, 2)
    assert len(rects) == 1
    rect = rects[0]
    assert rect.page == "1"
    assert rect.left == 10.0
    assert rect.top == 100.0
    assert rect.width == 70.0  # 60 + 20 - 10
    assert rect.height == 10.0


def test_two_lines_yield_two_rectangles() -> None:
    items = [
        _word(1, "a", 10.0, 100.0),
        _word(1, "b", 35.0, 100.0),
        _word(1, "c", 10.0, 80.0),
        _word(1, "d", 35.0, 80.0),
    ]
    rects = word_range_to_rectangles(items, 0, 3)
    assert len(rects) == 2
    # First (top) line.
    assert rects[0].top == 100.0
    assert rects[0].height == 10.0
    # Second (lower) line.
    assert rects[1].top == 80.0
    assert rects[1].height == 10.0


def test_backward_selection_matches_forward() -> None:
    items = [
        _word(1, "one", 10.0, 100.0),
        _word(1, "two", 35.0, 100.0),
        _word(1, "three", 60.0, 100.0),
    ]
    forward = word_range_to_rectangles(items, 0, 2)
    backward = word_range_to_rectangles(items, 2, 0)
    assert forward == backward


def test_single_word_rectangle() -> None:
    items = [_word(2, "solo", 50.0, 200.0, width=30.0, height=12.0)]
    rects = word_range_to_rectangles(items, 0, 0)
    assert len(rects) == 1
    assert rects[0].page == "2"
    assert rects[0].left == 50.0
    assert rects[0].width == 30.0


def test_out_of_range_raises() -> None:
    items = [_word(1, "a", 0.0, 0.0), _word(1, "b", 20.0, 0.0)]
    try:
        word_range_to_rectangles(items, 0, 5)
    except ValueError:
        return
    raise AssertionError("expected ValueError for out-of-range indices")


def test_selection_text_joins_words() -> None:
    items = [_word(1, "hello", 0.0, 0.0), _word(1, "world", 20.0, 0.0)]
    assert selection_text(items, 0, 1) == "hello world"
