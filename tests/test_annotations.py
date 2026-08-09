"""Annotations are the only rows in this database a person types, so the
validation and the write path get direct coverage."""

from datetime import date, timedelta

import pytest

from partner_dashboard import annotations as anno


def test_add_and_read_back(db):
    anno.add(db, on_date="2026-03-01", note="Raised the price to $19",
             author="ada@example.com")
    db.commit()
    rows = anno.recent(db)
    assert len(rows) == 1
    assert rows[0]["note"] == "Raised the price to $19"
    assert rows[0]["author"] == "ada@example.com"
    assert rows[0]["on_date"] == date(2026, 3, 1)


def test_newest_first_by_the_date_it_happened(db):
    """Ordered by when the thing happened, not when someone got round to
    recording it: a note added today about last March belongs in March."""
    anno.add(db, on_date="2026-06-01", note="later", author="a@example.com")
    anno.add(db, on_date="2026-01-01", note="earlier", author="a@example.com")
    db.commit()
    assert [r["note"] for r in anno.recent(db)] == ["later", "earlier"]


def test_by_month_keys_match_the_chart_labels(db):
    """The charts look a month up by its own label. If these two formats ever
    drift the markers silently stop appearing, with nothing failing."""
    anno.add(db, on_date="2026-03-14", note="price change", author="a@example.com")
    anno.add(db, on_date="2026-03-20", note="second thing", author="a@example.com")
    db.commit()
    grouped = anno.by_month(db)
    assert set(grouped) == {"Mar 2026"}
    assert [n["note"] for n in grouped["Mar 2026"]] == ["price change", "second thing"]

    # The same mask the monthly aggregates use.
    label = db.execute(
        "select to_char(date '2026-03-01', 'Mon YYYY')").fetchone()[0]
    assert label in grouped


@pytest.mark.parametrize("bad_date", ["", "14/03/2026", "2026-13-01", "nonsense", None])
def test_a_date_that_is_not_a_date_is_refused(db, bad_date):
    with pytest.raises(anno.AnnotationError):
        anno.add(db, on_date=bad_date, note="x", author="a@example.com")


def test_the_future_is_refused(db):
    ahead = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(anno.AnnotationError):
        anno.add(db, on_date=ahead, note="x", author="a@example.com")


def test_before_the_data_is_refused(db):
    with pytest.raises(anno.AnnotationError):
        anno.add(db, on_date="2001-01-01", note="x", author="a@example.com")


@pytest.mark.parametrize("bad_note", ["", "   ", None])
def test_an_empty_note_is_refused(db, bad_note):
    with pytest.raises(anno.AnnotationError):
        anno.add(db, on_date="2026-03-01", note=bad_note, author="a@example.com")


def test_an_overlong_note_is_refused_and_says_the_limit(db):
    with pytest.raises(anno.AnnotationError) as caught:
        anno.add(db, on_date="2026-03-01", note="x" * (anno.NOTE_MAX + 1),
                 author="a@example.com")
    assert str(anno.NOTE_MAX) in str(caught.value)


def test_a_note_at_the_limit_is_accepted(db):
    anno.add(db, on_date="2026-03-01", note="x" * anno.NOTE_MAX,
             author="a@example.com")
    db.commit()
    assert len(anno.recent(db)) == 1


def test_whitespace_is_trimmed(db):
    row = anno.add(db, on_date="2026-03-01", note="  padded  ",
                   author="a@example.com")
    assert row["note"] == "padded"


def test_remove_deletes_and_returns_what_went(db):
    row = anno.add(db, on_date="2026-03-01", note="typo", author="a@example.com")
    db.commit()
    gone = anno.remove(db, annotation_id=row["id"])
    db.commit()
    assert gone["note"] == "typo"
    assert anno.recent(db) == []


def test_remove_takes_the_id_as_a_string_from_a_form(db):
    """The id arrives out of a urlencoded body, so it is always text."""
    row = anno.add(db, on_date="2026-03-01", note="typo", author="a@example.com")
    db.commit()
    assert anno.remove(db, annotation_id=str(row["id"]))["note"] == "typo"


def test_remove_leaves_the_other_notes_alone(db):
    keep = anno.add(db, on_date="2026-03-01", note="keep", author="a@example.com")
    drop = anno.add(db, on_date="2026-03-02", note="drop", author="a@example.com")
    db.commit()
    anno.remove(db, annotation_id=drop["id"])
    db.commit()
    assert [r["id"] for r in anno.recent(db)] == [keep["id"]]


def test_removing_a_note_that_is_gone_returns_none_rather_than_raising(db):
    """Two people with the page open, both deleting: the second one is a
    no-op, not an error worth showing anybody."""
    assert anno.remove(db, annotation_id=999999) is None


@pytest.mark.parametrize("bad_id", ["", "seven", None, "1; drop table annotations"])
def test_a_non_numeric_id_is_refused(db, bad_id):
    with pytest.raises(anno.AnnotationError):
        anno.remove(db, annotation_id=bad_id)


def test_a_deleted_note_stops_marking_its_month(db):
    """The chart marker reads from by_month, so a deleted note has to vanish
    from there too or a dot outlives the note explaining it."""
    row = anno.add(db, on_date="2026-03-14", note="price change",
                   author="a@example.com")
    db.commit()
    assert "Mar 2026" in anno.by_month(db)
    anno.remove(db, annotation_id=row["id"])
    db.commit()
    assert anno.by_month(db) == {}
