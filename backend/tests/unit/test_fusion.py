import random
from fractions import Fraction

import pytest

from vault.core.config import RetrievalSettings
from vault.retrieval.fusion import FusedHit, FusionError, Hit, cap_per_document, fuse

# Synthetic document ids (ULIDs).
DOC_A = "01J00000000000000000000000"
DOC_B = "01J00000000000000000000001"
DOC_C = "01J00000000000000000000002"

DEFAULT = RetrievalSettings()


def settings(**values: int) -> RetrievalSettings:
    return RetrievalSettings.model_validate(values)


def hits(*pairs: tuple[int, str]) -> list[Hit]:
    return [Hit(chunk, document) for chunk, document in pairs]


def test_defaults_are_the_plans() -> None:
    assert DEFAULT.keyword_candidates == 50  # K_kw
    assert DEFAULT.vector_candidates == 50  # K_vec
    assert DEFAULT.rrf_k == 60
    assert DEFAULT.max_chunks_per_document == 3  # M


def test_hand_computed_scores() -> None:
    # Keyword: 1, 2, 3. Vector: 3, 4, 1. k = 60.
    fused = fuse(
        hits((1, DOC_A), (2, DOC_A), (3, DOC_B)),
        hits((3, DOC_B), (4, DOC_C), (1, DOC_A)),
        DEFAULT,
    )
    scores = {hit.chunk_id: hit.score for hit in fused}
    assert scores == {
        1: Fraction(1, 61) + Fraction(1, 63),
        2: Fraction(1, 62),
        3: Fraction(1, 63) + Fraction(1, 61),
        4: Fraction(1, 62),
    }
    # 1 and 3 tie exactly, as do 2 and 4: ties go to the lower chunk id.
    assert [hit.chunk_id for hit in fused] == [1, 3, 2, 4]
    assert fused[0] == FusedHit(1, DOC_A, Fraction(1, 61) + Fraction(1, 63), 1, 3)
    assert fused[3] == FusedHit(4, DOC_C, Fraction(1, 62), None, 2)


def test_rrf_k_comes_from_the_settings() -> None:
    fused = fuse(hits((5, DOC_A)), hits((5, DOC_A)), settings(rrf_k=0))
    assert fused[0].score == Fraction(2)
    fused = fuse(hits((5, DOC_A)), [], settings(rrf_k=10))
    assert fused[0].score == Fraction(1, 11)


def test_a_hit_in_both_lists_beats_one_in_a_single_list_at_the_same_rank() -> None:
    fused = fuse(hits((1, DOC_A), (2, DOC_B)), hits((2, DOC_B)), DEFAULT)
    assert [hit.chunk_id for hit in fused] == [2, 1]
    assert (fused[0].keyword_rank, fused[0].vector_rank) == (2, 1)


def test_hits_in_only_one_list() -> None:
    keyword_only = fuse(hits((7, DOC_A), (8, DOC_B)), [], DEFAULT)
    assert [(h.chunk_id, h.score, h.keyword_rank, h.vector_rank) for h in keyword_only] == [
        (7, Fraction(1, 61), 1, None),
        (8, Fraction(1, 62), 2, None),
    ]
    vector_only = fuse([], hits((8, DOC_B), (7, DOC_A)), DEFAULT)
    assert [(h.chunk_id, h.keyword_rank, h.vector_rank) for h in vector_only] == [
        (8, None, 1),
        (7, None, 2),
    ]


def test_equal_ranks_in_different_lists_tie_by_chunk_id() -> None:
    fused = fuse(hits((9, DOC_A)), hits((4, DOC_B)), DEFAULT)
    assert [hit.chunk_id for hit in fused] == [4, 9]
    assert fused[0].score == fused[1].score


def test_exact_ties_of_different_ranks() -> None:
    # k = 0. Chunk 10 at ranks 3 and 6 (1/3 + 1/6), chunk 11 at ranks 4 and 4 (1/4 + 1/4).
    keyword = hits((1, DOC_A), (2, DOC_A), (10, DOC_B), (11, DOC_C))
    vector = hits((3, DOC_A), (4, DOC_A), (5, DOC_A), (11, DOC_C), (6, DOC_A), (10, DOC_B))
    fused = {hit.chunk_id: hit for hit in fuse(keyword, vector, settings(rrf_k=0))}
    assert fused[10].score == fused[11].score == Fraction(1, 2)
    order = [hit.chunk_id for hit in fuse(keyword, vector, settings(rrf_k=0))]
    assert order.index(10) == order.index(11) - 1


def test_the_order_does_not_depend_on_the_input_order_of_ties() -> None:
    keyword = hits((30, DOC_A), (31, DOC_B), (32, DOC_C))
    vector = hits((33, DOC_A), (34, DOC_B), (35, DOC_C))
    expected = fuse(keyword, vector, DEFAULT)
    assert [hit.chunk_id for hit in expected] == [30, 33, 31, 34, 32, 35]
    assert fuse(vector, keyword, DEFAULT) == tuple(
        FusedHit(h.chunk_id, h.document_id, h.score, h.vector_rank, h.keyword_rank)
        for h in expected
    )


def test_the_ranking_is_sorted_and_deterministic_on_generated_lists() -> None:
    generator = random.Random(8)  # noqa: S311 (test data, not a secret)
    documents = [DOC_A, DOC_B, DOC_C]
    owner = {chunk: documents[chunk % 3] for chunk in range(1, 40)}
    for _ in range(300):
        keyword = [
            Hit(c, owner[c]) for c in generator.sample(range(1, 40), generator.randint(0, 20))
        ]
        vector = [
            Hit(c, owner[c]) for c in generator.sample(range(1, 40), generator.randint(0, 20))
        ]
        config = settings(rrf_k=generator.randint(0, 5))
        fused = fuse(keyword, vector, config)
        assert fused == fuse(list(keyword), list(vector), config)
        keys = [(-hit.score, hit.chunk_id) for hit in fused]
        assert keys == sorted(keys)
        assert len(fused) == len({h.chunk_id for h in keyword} | {h.chunk_id for h in vector})
        for hit in fused:
            expected = sum(
                (
                    Fraction(1, config.rrf_k + r)
                    for r in (hit.keyword_rank, hit.vector_rank)
                    if r is not None
                ),
                Fraction(0),
            )
            assert hit.score == expected
            if hit.keyword_rank is not None:
                assert keyword[hit.keyword_rank - 1].chunk_id == hit.chunk_id


def test_a_repeat_keeps_the_first_place_and_takes_no_place() -> None:
    fused = fuse(hits((1, DOC_A), (2, DOC_B), (1, DOC_A), (3, DOC_C)), [], DEFAULT)
    assert [(hit.chunk_id, hit.keyword_rank) for hit in fused] == [(1, 1), (2, 2), (3, 3)]


def test_a_repeat_does_not_use_up_a_candidate() -> None:
    config = settings(keyword_candidates=2)
    fused = fuse(hits((1, DOC_A), (1, DOC_A), (2, DOC_B), (3, DOC_C)), [], config)
    assert [hit.chunk_id for hit in fused] == [1, 2]


def test_only_the_configured_number_of_candidates_is_read_from_each_list() -> None:
    keyword = hits((1, DOC_A), (2, DOC_A), (3, DOC_A))
    vector = hits((4, DOC_B), (5, DOC_B), (3, DOC_A))
    fused = fuse(keyword, vector, settings(keyword_candidates=2, vector_candidates=2))
    assert sorted(hit.chunk_id for hit in fused) == [1, 2, 4, 5]
    fused = fuse(keyword, vector, settings(keyword_candidates=3, vector_candidates=3))
    assert fused[0].chunk_id == 3  # in both lists once both reach it


def test_empty_inputs() -> None:
    assert fuse([], [], DEFAULT) == ()
    assert cap_per_document((), DEFAULT) == ()


def test_inputs_may_be_iterators() -> None:
    fused = fuse(iter(hits((1, DOC_A))), (hit for hit in hits((1, DOC_A))), DEFAULT)
    assert fused[0].score == Fraction(2, 61)


@pytest.mark.parametrize(
    ("keyword", "vector"),
    [
        (hits((1, DOC_A), (1, DOC_B)), []),  # within one list
        (hits((1, DOC_A)), hits((1, DOC_B))),  # across the lists
        (hits((1, DOC_A), (2, DOC_A)), hits((1, DOC_B))),
    ],
)
def test_a_chunk_with_two_documents_is_refused(keyword: list[Hit], vector: list[Hit]) -> None:
    with pytest.raises(FusionError, match="two different documents"):
        fuse(keyword, vector, DEFAULT)


def test_hits_past_the_candidate_count_are_checked_too() -> None:
    with pytest.raises(FusionError, match="two different documents"):
        fuse(hits((1, DOC_A), (2, DOC_B), (1, DOC_C)), [], settings(keyword_candidates=1))
    with pytest.raises(FusionError, match="must hold hits"):
        fuse([Hit(1, DOC_A), (2, DOC_B)], [], settings(keyword_candidates=1))  # type: ignore[list-item]


@pytest.mark.parametrize("value", ["not a hit", (1, DOC_A), None, 1])
def test_a_list_must_hold_hits(value: object) -> None:
    with pytest.raises(FusionError, match="must hold hits"):
        fuse([value], [], DEFAULT)  # type: ignore[list-item]
    with pytest.raises(FusionError, match="must hold hits"):
        fuse([], [value], DEFAULT)  # type: ignore[list-item]


def test_a_string_is_not_a_list_of_hits() -> None:
    with pytest.raises(FusionError):
        fuse("12", [], DEFAULT)  # type: ignore[arg-type]


@pytest.mark.parametrize("chunk_id", [0, -1, True, False, 1.0, "1", None])
def test_a_chunk_id_must_be_a_whole_number_from_1(chunk_id: object) -> None:
    with pytest.raises(FusionError, match="chunk id"):
        Hit(chunk_id, DOC_A)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "document_id",
    ["", "01j00000000000000000000000", DOC_A + "\n", DOC_A[:-1], "81J00000000000000000000000", 1],
)
def test_a_document_id_must_be_a_ulid(document_id: object) -> None:
    with pytest.raises(FusionError, match="ULID"):
        Hit(1, document_id)  # type: ignore[arg-type]


def test_cap_keeps_the_best_m_chunks_of_each_document_in_order() -> None:
    keyword = hits((1, DOC_A), (2, DOC_A), (3, DOC_B), (4, DOC_A), (5, DOC_A), (6, DOC_B))
    fused = fuse(keyword, [], settings(max_chunks_per_document=2))
    capped = cap_per_document(fused, settings(max_chunks_per_document=2))
    assert [hit.chunk_id for hit in capped] == [1, 2, 3, 6]
    assert [hit.chunk_id for hit in cap_per_document(fused, DEFAULT)] == [1, 2, 3, 4, 6]
    assert [
        hit.chunk_id for hit in cap_per_document(fused, settings(max_chunks_per_document=1))
    ] == [
        1,
        3,
    ]


def test_cap_leaves_a_ranking_within_the_cap_unchanged() -> None:
    fused = fuse(hits((1, DOC_A), (2, DOC_B), (3, DOC_C)), hits((3, DOC_C)), DEFAULT)
    assert cap_per_document(fused, DEFAULT) == fused


def test_cap_refuses_a_chunk_listed_twice_and_anything_but_fused_hits() -> None:
    fused = fuse(hits((1, DOC_A)), [], DEFAULT)
    with pytest.raises(FusionError, match="twice"):
        cap_per_document(fused + fused, DEFAULT)
    with pytest.raises(FusionError, match="fused hits"):
        cap_per_document(hits((1, DOC_A)), DEFAULT)  # type: ignore[arg-type]


def test_messages_name_no_id() -> None:
    with pytest.raises(FusionError) as raised:
        fuse(hits((12345, DOC_A)), hits((12345, DOC_B)), DEFAULT)
    assert "12345" not in str(raised.value)
    assert DOC_A not in str(raised.value)
    assert DOC_B not in str(raised.value)
