"""
Tests for the CV parsing primitives in library/ravendb_diagnostic.py.

A RavenDB change vector looks like:   A:1234-FzoY3k+4jkSVbEYRBvhzFQ
                                      tag:etag-dbid
Multi-entry:                          A:1-X, B:2-Y
New-form (pipe lane separator):       A:1-X|B:2-Y
"""

import ravendb_diagnostic as diag


def test_parse_cv_entries_empty_string():
    assert diag.parse_cv_entries("") == []


def test_parse_cv_entries_none():
    assert diag.parse_cv_entries(None) == []


def test_parse_cv_entries_one_entry():
    assert diag.parse_cv_entries("A:1234-FzoY3k+4jkSVbEYRBvhzFQ") == [
        ("A", "FzoY3k+4jkSVbEYRBvhzFQ"),
    ]


def test_parse_cv_entries_two_entries_comma_separated():
    cv = "A:1-X, B:2-Y"
    assert diag.parse_cv_entries(cv) == [("A", "X"), ("B", "Y")]


def test_parse_cv_entries_pipe_separator():
    # New-form uses '|' to separate lanes; parser treats it like a comma.
    cv = "A:1-X|B:2-Y"
    assert diag.parse_cv_entries(cv) == [("A", "X"), ("B", "Y")]


def test_parse_cv_entries_drops_the_etag():
    # The (tag, dbid) tuple does NOT include etag. Same dbid + different etag
    # = same tuple. (etag is captured by _parse_cv_set, not by this one.)
    assert diag.parse_cv_entries("A:1-X") == diag.parse_cv_entries("A:9999-X")


def test_parse_cv_entries_skips_garbage_between_entries():
    # findall skips non-matches. A partially malformed CV won't raise -- it'll
    # silently parse only the well-formed entries. Worth knowing.
    assert diag.parse_cv_entries("junk A:1-X more junk B:2-Y") == [
        ("A", "X"), ("B", "Y"),
    ]


# ---- _parse_cv_set: returns set of (dbid, etag_int) tuples ------------------
# (cross_cluster_cv_equality uses this for set-equality comparisons across nodes)

def test_parse_cv_set_empty_string():
    assert diag._parse_cv_set("") == set()


def test_parse_cv_set_none():
    assert diag._parse_cv_set(None) == set()


def test_parse_cv_set_one_entry_etag_is_int():
    # Critical: the etag in the tuple is an int, not a str.
    assert diag._parse_cv_set("A:1-X") == {("X", 1)}


def test_parse_cv_set_order_doesnt_matter():
    assert diag._parse_cv_set("A:1-X, B:2-Y") == diag._parse_cv_set("B:2-Y, A:1-X")


def test_parse_cv_set_pipe_and_comma_produce_the_same_set():
    # The pipe lane separator is invisible in set form: both halves contribute
    # entries to one combined set.
    assert diag._parse_cv_set("A:1-X|B:2-Y") == diag._parse_cv_set("A:1-X, B:2-Y")


def test_parse_cv_set_tag_is_not_part_of_the_key():
    # (dbid, etag) is the key; the tag is dropped. So "A:1-X" and "SINK:1-X"
    # collapse to the same entry. This is the right behavior for cross-cluster
    # checks (different nodes can name the same write differently).
    assert diag._parse_cv_set("A:1-X") == diag._parse_cv_set("SINK:1-X")


def test_parse_cv_set_silently_drops_unparseable_etag():
    # A non-digit etag fails the regex match in the first place, so the
    # entry just disappears. A wholly malformed CV looks the same as an
    # empty one. PHASE 2: is that the right behavior, or should we surface it?
    assert diag._parse_cv_set("A:notanumber-X") == set()


# ---- the raw regexes the parsers sit on top of -----------------------------

def test_dbid_regex_returns_pairs():
    assert diag._CV_DBID_RE.findall("A:1-X, B:2-Y") == [("A", "X"), ("B", "Y")]


def test_full_regex_returns_triples():
    # Three groups: (tag, etag_as_str, dbid). _parse_cv_set converts etag to int.
    assert diag._CV_ENTRY_FULL_RE.findall("A:1-X, B:22-Y") == [
        ("A", "1", "X"), ("B", "22", "Y"),
    ]


def test_full_regex_rejects_negative_etag():
    # Etag pattern is \d+ -- the '-' after ':' breaks the match.
    assert diag._CV_ENTRY_FULL_RE.findall("A:-1-X") == []


def test_full_regex_rejects_decimal_etag():
    assert diag._CV_ENTRY_FULL_RE.findall("A:1.5-X") == []


def test_full_regex_rejects_missing_pieces():
    assert diag._CV_ENTRY_FULL_RE.findall("") == []
    assert diag._CV_ENTRY_FULL_RE.findall(":1-X") == []      # no tag
    assert diag._CV_ENTRY_FULL_RE.findall("A-X") == []        # no :etag-
    assert diag._CV_ENTRY_FULL_RE.findall("A:1") == []        # no -dbid
