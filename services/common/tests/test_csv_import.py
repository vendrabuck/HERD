"""Unit tests for the shared import CSV reader (issue #1574).

Before the reader existed, each importer wrapped its text in `io.StringIO(text)`
and iterated `csv.DictReader` with no handler: a cell over the csv module's field
limit (131,072 characters by default) or a bare carriage return outside quotes
raised `csv.Error`, and the route answered 500.
"""

import csv

import pytest
from fastapi import HTTPException
from herd_common import csv_import
from herd_common.csv_import import CSV_UNREADABLE_DETAIL, read_csv_rows

_DEFAULT_FIELD_LIMIT = 131_072


@pytest.fixture(autouse=True)
def _default_field_limit():
    """Every test starts and must end at the module's default limit."""
    assert csv.field_size_limit() == _DEFAULT_FIELD_LIMIT
    yield
    assert csv.field_size_limit() == _DEFAULT_FIELD_LIMIT


def test_the_detail_is_fixed_and_names_only_a_line():
    assert CSV_UNREADABLE_DETAIL.format(line=7) == (
        "Invalid CSV: line 7 could not be read; fix it and retry"
    )


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_every_line_ending_reads_the_same_rows(ending):
    text = ending.join(["name,model", "a,m1", "b,m2"]) + ending
    assert read_csv_rows(text) == [
        {"name": "a", "model": "m1"},
        {"name": "b", "model": "m2"},
    ]


def test_a_cr_only_file_without_a_final_line_end_reads():
    assert read_csv_rows("name,model\ra,m1\rb,m2") == [
        {"name": "a", "model": "m1"},
        {"name": "b", "model": "m2"},
    ]


def test_a_bare_cr_in_an_unquoted_cell_ends_the_line():
    """The way a spreadsheet reads it: the cell's text after the CR starts a new row."""
    assert read_csv_rows("name,model\r\na\rb,m1\r\n") == [
        {"name": "a", "model": None},
        {"name": "b", "model": "m1"},
    ]


@pytest.mark.parametrize("brk", ["\r", "\n", "\r\n"], ids=["cr", "lf", "crlf"])
def test_a_line_break_inside_a_quoted_cell_is_kept(brk):
    assert read_csv_rows(f'name,model\n"a{brk}b",m1\n') == [{"name": f"a{brk}b", "model": "m1"}]


@pytest.mark.parametrize("length", [_DEFAULT_FIELD_LIMIT, _DEFAULT_FIELD_LIMIT + 1, 1_000_000])
@pytest.mark.parametrize("quoted", [False, True], ids=["unquoted", "quoted"])
def test_a_cell_past_the_default_field_limit_reads(length, quoted):
    cell = "x" * length
    written = f'"{cell}"' if quoted else cell
    rows = read_csv_rows(f"name,model\n{written},m1\n")
    assert len(rows) == 1
    assert rows[0]["model"] == "m1"
    assert len(rows[0]["name"]) == length


def test_a_header_cell_past_the_default_field_limit_reads():
    header = "h" * (_DEFAULT_FIELD_LIMIT + 1)
    assert read_csv_rows(f"{header}\nv\n") == [{header: "v"}]


def test_the_field_limit_is_the_text_length_and_never_lowered():
    assert csv_import._field_limit_for("") == _DEFAULT_FIELD_LIMIT
    text = "x" * (_DEFAULT_FIELD_LIMIT + 5)
    assert csv_import._field_limit_for(text) == len(text)


def test_the_limit_is_raised_only_during_the_read(monkeypatch):
    seen: list[int] = []
    real_dict_reader = csv.DictReader

    class _Spy(real_dict_reader):
        def __next__(self):
            seen.append(csv.field_size_limit())
            return super().__next__()

    monkeypatch.setattr(csv_import.csv, "DictReader", _Spy)
    text = "name\n" + "x" * 200_000 + "\n"
    read_csv_rows(text)
    assert seen and set(seen) == {len(text)}
    assert csv.field_size_limit() == _DEFAULT_FIELD_LIMIT


def _refusing(monkeypatch, limit: int) -> None:
    """Make the csv module itself refuse any cell longer than `limit`."""
    monkeypatch.setattr(csv_import, "_field_limit_for", lambda text: limit)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("name_is_long,model\na,m1\n", 1),
        ("name,model\nabcdefghijkl,m1\n", 2),
        ("name,model\na,m1\nb,m2\nabcdefghijkl,m3\n", 4),
        ('name,model\n"a\nb\nc",m1\nabcdefghijkl,m2\n', 5),
        ("name,model\ra,m1\rabcdefghijkl,m2\r", 3),
        ('name,model\n"abcdefghijkl",m1\n', 2),
    ],
    ids=["header", "first_row", "fourth_line", "after_a_multiline_cell", "cr_lines", "quoted"],
)
def test_a_csv_error_answers_the_fixed_422_naming_the_line(monkeypatch, text, line):
    _refusing(monkeypatch, 8)
    with pytest.raises(HTTPException) as exc:
        read_csv_rows(text)
    assert exc.value.status_code == 422
    assert exc.value.detail == f"Invalid CSV: line {line} could not be read; fix it and retry"
    # Nothing of the module's own error, nor of the file, rides along.
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    assert "abcdefghijkl" not in exc.value.detail
    assert csv.field_size_limit() == _DEFAULT_FIELD_LIMIT


def test_a_csv_error_puts_the_previous_limit_back(monkeypatch):
    _refusing(monkeypatch, 8)
    with pytest.raises(HTTPException):
        read_csv_rows("name\nabcdefghijkl\n")
    assert csv.field_size_limit() == _DEFAULT_FIELD_LIMIT


def test_the_limit_is_put_back_when_the_read_raises_anything_else(monkeypatch):
    class _Boom(csv.DictReader):
        def __next__(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(csv_import.csv, "DictReader", _Boom)
    with pytest.raises(RuntimeError):
        read_csv_rows("name\n" + "x" * 200_000 + "\n")
    assert csv.field_size_limit() == _DEFAULT_FIELD_LIMIT


# What the csv module reads without complaint, pinned so a change in a later Python
# shows up here rather than as a surprise in an importer.
@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("", []),
        ("name,model\n", []),
        ("name,model\na\x00b,m1\n", [{"name": "a\x00b", "model": "m1"}]),
        ('name,model\n"abc,m1\n', [{"name": "abc,m1\n", "model": None}]),
        ('name,model\n"abc', [{"name": "abc", "model": None}]),
        ('name,model\nab"c,m1\n', [{"name": 'ab"c', "model": "m1"}]),
        ('name,model\n"ab"c,m1\n', [{"name": "abc", "model": "m1"}]),
        ("name,model\na,m1,extra\n", [{"name": "a", "model": "m1", None: ["extra"]}]),
        ("name,model\na b\x85c,m1\n", [{"name": "a b\x85c", "model": "m1"}]),
    ],
    ids=[
        "empty",
        "header_only",
        "nul",
        "unterminated_quote",
        "unterminated_quote_at_eof",
        "quote_inside_unquoted_cell",
        "text_after_a_closing_quote",
        "extra_cell",
        "unicode_line_separators",
    ],
)
def test_input_the_csv_module_reads_leniently(text, rows):
    assert read_csv_rows(text) == rows
