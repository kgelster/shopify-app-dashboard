"""Dated notes that draw on the charts. Read and write.

Lives outside stats.py because stats.py is read-side aggregates and this is the
one place the dashboard writes something a person typed. Keeping the write in
its own module means the validation has one home rather than being spread
between a route and a query.
"""

from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row

from partner_dashboard.config import get_settings

# Long enough for "raised the price and grandfathered everyone below 40
# installs", short enough that nobody writes an essay into a chart marker. The
# cap is enforced here rather than by a column type so the error can say what
# the limit is.
NOTE_MAX = 280

# Nothing before the app existed, and nothing in the future: a chart marker
# dated 2087 is either a typo or someone testing, and both are worth refusing.
# The floor is ANNOTATIONS_EARLIEST; set it to roughly when your app launched.


class AnnotationError(ValueError):
    """A note that cannot be stored, with a message written for the person who
    typed it rather than for a log."""


def _clean_date(value) -> date:
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            raise AnnotationError("Give the date as YYYY-MM-DD.") from None
    earliest = get_settings().annotations_earliest
    if parsed < earliest:
        raise AnnotationError(f"That is before {earliest.isoformat()}, which is "
                              "earlier than any data here.")
    if parsed > date.today():
        raise AnnotationError("That date is in the future.")
    return parsed


def _clean_note(value: str | None) -> str:
    note = (value or "").strip()
    if not note:
        raise AnnotationError("Write what happened.")
    if len(note) > NOTE_MAX:
        raise AnnotationError(f"Keep it under {NOTE_MAX} characters; that was "
                              f"{len(note)}.")
    return note


def add(conn: psycopg.Connection, *, on_date, note: str | None, author: str) -> dict:
    """Store one note. Raises AnnotationError with a readable message.

    `author` comes from the verified session, never from the form: a field the
    browser supplies is a field anyone can set.
    """
    row = conn.execute(
        """insert into annotations (on_date, note, author)
           values (%s, %s, %s)
           returning id, on_date, note, author, created_at""",
        (_clean_date(on_date), _clean_note(note), author),
    ).fetchone()
    return {"id": row[0], "on_date": row[1], "note": row[2],
            "author": row[3], "created_at": row[4]}


def _clean_id(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise AnnotationError("That note id is not a number.") from None


def remove(conn: psycopg.Connection, *, annotation_id) -> dict | None:
    """Delete one note, returning the row that went, or None if it was already
    gone.

    A hard delete rather than a `deleted_at` flag. This is a handful of notes a
    person typed on an internal dashboard, not an audit log: a soft-delete
    column would put a `where deleted_at is null` on every read here and in
    `by_month`, and the first one anybody forgot would resurrect a note onto a
    chart. Re-adding a note costs one line of typing, so permanence buys
    nothing and costs a stuck typo.

    Any signed-in reader may delete any note, including one they did not write.
    Restricting it to the author would make the bulk-imported changelog rows
    -- the ones most likely to need correcting -- undeletable by anyone.
    """
    row = conn.execute(
        """delete from annotations where id = %s
           returning id, on_date, note, author""",
        (_clean_id(annotation_id),),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "on_date": row[1], "note": row[2], "author": row[3]}


def recent(conn: psycopg.Connection, limit: int = 100) -> list[dict]:
    """Newest first, by the date the thing happened."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """select id, on_date, note, author, created_at from annotations
           order by on_date desc, id desc limit %s""",
        (limit,),
    )
    return cur.fetchall()


def by_month(conn: psycopg.Connection) -> dict[str, list[dict]]:
    """Keyed by the same 'Mon YYYY' label the monthly charts use.

    Keyed on the label rather than on a date so the template can look a month up
    without recomputing anything. If the chart's label format ever changes, this
    has to change with it -- which is why both use to_char with the same mask
    rather than one formatting in SQL and the other in Python.
    """
    rows = conn.execute(
        """select to_char(on_date, 'Mon YYYY'), on_date, note, author
           from annotations order by on_date, id"""
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for label, on_date, note, author in rows:
        out.setdefault(label, []).append(
            {"on_date": on_date, "note": note, "author": author}
        )
    return out
