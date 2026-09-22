"""Isolated unit tests for the DocumentCache (no I/O, no mocks)."""

from uwazi_property_filler.domain.document_cache import DocumentCache
from uwazi_property_filler.domain.fill_status import FillStatus
from uwazi_property_filler.domain.pdf_item import PdfItem


def _item(shared_id: str, title: str = "") -> PdfItem:
    return PdfItem(shared_id=shared_id, title=title, template_name="DOCUMENT")


def _cache() -> DocumentCache:
    cache = DocumentCache(template="DOCUMENT", language="en")
    cache.pending[None] = [_item("a"), _item("b")]
    cache.validated[None] = [_item("c")]
    cache.pending["v1"] = [_item("a")]
    cache.validated["v1"] = [_item("c")]
    cache.pending["v2"] = [_item("b")]
    return cache


def test_split_returns_copies_per_bucket() -> None:
    cache = _cache()
    pending, validated = cache.split(None)
    assert [i.shared_id for i in pending] == ["a", "b"]
    assert [i.shared_id for i in validated] == ["c"]
    assert cache.split("v2") == ([_item("b").model_copy(update={"template_name": "DOCUMENT"})], [])
    # copies: mutating the returned list does not touch the cache
    pending.clear()
    assert len(cache.pending[None]) == 2


def test_split_unknown_bucket_is_empty() -> None:
    cache = _cache()
    assert cache.split("nope") == ([], [])


def test_count_sums_pending_and_validated() -> None:
    cache = _cache()
    assert cache.count(None) == 3
    assert cache.count("v1") == 2
    assert cache.count("v2") == 1


def test_values_lists_distinct_non_none_keys() -> None:
    assert _cache().values() == ["v1", "v2"]


def test_total_sums_all_buckets() -> None:
    assert _cache().total() == 6


def test_mark_validated_moves_every_bucket_holding_item() -> None:
    cache = _cache()
    cache.mark_validated("a")
    assert [i.shared_id for i in cache.pending[None]] == ["b"]
    assert [i.shared_id for i in cache.validated[None]] == ["c", "a"]
    assert cache.pending["v1"] == []
    assert [i.shared_id for i in cache.validated["v1"]] == ["c", "a"]
    assert cache.count(None) == 3  # totals unchanged, only moved


def test_mark_validated_unknown_id_is_noop() -> None:
    cache = _cache()
    cache.mark_validated("zzz")
    assert [i.shared_id for i in cache.pending[None]] == ["a", "b"]
    assert [i.shared_id for i in cache.validated[None]] == ["c"]


def _snapshot_item(shared_id: str, status: FillStatus = FillStatus.PENDING, subtitle: str = "") -> PdfItem:
    return PdfItem(
        shared_id=shared_id,
        title=f"Doc {shared_id}",
        subtitle=subtitle,
        template_name="DOCUMENT",
        filename=f"{shared_id}.pdf",
        status=status,
    )


def test_snapshot_round_trip_restores_buckets_and_statuses() -> None:
    cache = DocumentCache(template="DOCUMENT", language="en")
    cache.pending[None] = [_snapshot_item("a")]
    cache.pending["Resolutions"] = [_snapshot_item("b", subtitle="A/RES/4")]
    cache.validated[None] = [_snapshot_item("c", status=FillStatus.VALIDATED)]

    restored = DocumentCache.from_json(cache.to_json())

    assert restored is not None
    assert (restored.template, restored.language) == ("DOCUMENT", "en")
    assert restored.split(None) == (cache.pending[None], cache.validated[None])
    assert restored.split("Resolutions") == (cache.pending["Resolutions"], [])
    assert restored.split("Resolutions")[0][0].subtitle == "A/RES/4"
    assert restored.split(None)[1][0].status is FillStatus.VALIDATED
    assert restored.count(None) == 2
    assert restored.count("Resolutions") == 1


def test_snapshot_restores_none_as_the_all_bucket_key() -> None:
    # JSON object keys are strings: the ``None`` (ALL) bucket must map back.
    cache = DocumentCache(template="DOCUMENT", language="en")
    cache.pending[None] = [_snapshot_item("a")]
    restored = DocumentCache.from_json(cache.to_json())
    assert restored is not None
    assert restored.split(None)[0][0].shared_id == "a"


def test_from_json_rejects_corrupt_input() -> None:
    assert DocumentCache.from_json("{not json") is None
    assert DocumentCache.from_json("") is None


def test_from_json_rejects_wrong_shape() -> None:
    assert DocumentCache.from_json('{"template": "DOCUMENT"}') is None
    assert DocumentCache.from_json('{"template": "DOCUMENT", "language": "en", "pending": "oops"}') is None
    assert (
        DocumentCache.from_json(
            '{"template": "DOCUMENT", "language": "en", "pending": [{"value": null, "items": [{"bad": 1}]}]}'
        )
        is None
    )
