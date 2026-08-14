from utils.fleet_roster import parse_roster


def test_reads_the_three_columns():
    rows, problems = parse_roster("4024  ST510278  AHMED, IBRAHIM HASHI / ABDULLAHI, ABDI")
    assert rows == [{"unit": "4024", "trailer": "ST510278"}]
    assert problems == []


def test_a_two_word_trailer_stays_whole():
    # Single-spaced words inside a column are why the split is on 2+ spaces:
    # "BOX TRUCK" and "671316 LOT" are one trailer each, not a trailer and a name.
    rows, _ = parse_roster(
        "396497  BOX TRUCK  HORTON, ROBERT (ML) SOLO\n"
        "2432  671316 LOT  DEANS, DAVID (ML)"
    )
    assert [r["trailer"] for r in rows] == ["BOX TRUCK", "671316 LOT"]


def test_units_are_not_always_digits():
    # "1002FT" and "F9121" are real units, and 0942's leading zero is real too --
    # the digit-run parsing used for titles would mangle all three.
    rows, problems = parse_roster(
        "1002FT     ST507646  ANTOINE, JOEL MATHURIN / HYPPOLITE, MONIA\n"
        "F9121  2437742PLA  BOYKIN, LEON / MARTINEZ HERNANDEZ, MARIO (ML)\n"
        "0942  ST510244  JOHNSON, KENNETH / ROUNDTREE, FLOYD L"
    )
    assert [r["unit"] for r in rows] == ["1002FT", "F9121", "0942"]
    assert problems == []


def test_ragged_spacing_is_still_three_columns():
    rows, _ = parse_roster("203816   PTLZ252345  MOHAMED, ABDIFATAH ISMAIL / ABDULLE, OSMAN")
    assert rows == [{"unit": "203816", "trailer": "PTLZ252345"}]


def test_a_short_line_is_reported_not_dropped():
    # A silently skipped line is a truck that can never be onboarded.
    rows, problems = parse_roster("2464 PO NOUR, OMER AHMED / DUALE, MOHAMED")
    assert rows == []
    assert len(problems) == 1 and "3 columns" in problems[0]["reason"]


def test_a_name_in_the_trailer_column_is_a_problem():
    rows, problems = parse_roster("2464    NOUR, OMER AHMED    DUALE, MOHAMED")
    assert rows == []
    assert "name" in problems[0]["reason"]


def test_the_legend_line_is_not_a_truck():
    _, problems = parse_roster("truck unit#  trailer#  1st driver name / 2nd driver name")
    assert len(problems) == 1


def test_a_repeated_unit_is_reported_rather_than_overwritten():
    # The two lines disagree about the trailer; picking one quietly is the guess
    # this whole module exists to avoid.
    rows, problems = parse_roster(
        "4024  ST510278  AHMED, IBRAHIM HASHI / ABDULLAHI, ABDI\n"
        "4024  SM510425  SOMEONE, ELSE / OTHER, PERSON"
    )
    assert rows == [{"unit": "4024", "trailer": "ST510278"}]
    assert "already listed" in problems[0]["reason"]


def test_blank_lines_are_not_problems():
    rows, problems = parse_roster("\n\n4024  ST510278  AHMED, IBRAHIM\n\n")
    assert len(rows) == 1 and problems == []
