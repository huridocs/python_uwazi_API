"""Isolated unit tests for the pure batch splitter (per AGENTS.md)."""

import pytest

from uwazi_admin_agent.domain.batch_split import split_batches


def test_splits_into_fixed_chunks_preserving_order() -> None:
    chunks = split_batches(list(range(10)), 4)
    assert chunks == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]


def test_exact_multiple_leaves_no_empty_tail_chunk() -> None:
    assert split_batches(list(range(6)), 3) == [[0, 1, 2], [3, 4, 5]]


def test_size_at_least_the_list_makes_one_chunk() -> None:
    assert split_batches(["a", "b"], 50) == [["a", "b"]]


def test_empty_input_makes_no_chunks() -> None:
    assert split_batches([], 4) == []


def test_chunks_share_references_not_copies() -> None:
    """A chunk must hold the same item objects — no data copied at bulk scale."""
    entity = {"shared_id": "A"}
    chunks = split_batches([entity], 1)
    assert chunks[0][0] is entity


def test_non_positive_size_fails_loudly() -> None:
    with pytest.raises(ValueError):
        split_batches([1, 2, 3], 0)
    with pytest.raises(ValueError):
        split_batches([1], -1)
