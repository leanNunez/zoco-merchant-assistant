from evaluation.retrieval.provenance import corpus_digest


def test_the_digest_hashes_path_length_and_bytes_of_each_file_in_path_order() -> None:
    # printf 'a.md\0\061\0xb.md\0\062\0yz' | sha256sum ("\061" is "1"; input order is irrelevant)
    assert corpus_digest([("b.md", b"yz"), ("a.md", b"x")]) == (
        "d41288a4d802e1a67c9a184109b722a233706cd8c825ccf89dad4f297df2d576"
    )


def test_renaming_a_file_changes_the_digest() -> None:
    assert corpus_digest([("a.md", b"x")]) != corpus_digest([("c.md", b"x")])


def test_moving_bytes_across_a_file_boundary_changes_the_digest() -> None:
    assert corpus_digest([("a", b"xy"), ("b", b"z")]) != corpus_digest([("a", b"x"), ("b", b"yz")])
