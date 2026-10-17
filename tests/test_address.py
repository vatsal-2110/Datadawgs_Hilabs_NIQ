"""Unit tests for the shared address standardizer (src/common/address.py).  Run: python -m pytest tests -q"""
import math

import pytest

from src.common.address import building_key, normalize_zip, standardize_address


def std(*args, **kw):
    return standardize_address(*args, **kw)


# --- the same building written differently must get the same key ---------------------------------
SAME_PLACE = [
    [("123 North Main Street", "Suite 200", "Los Angeles", "CA", "900121234"),
     ("123 N. MAIN ST.", "STE 200", "LOS ANGELES", "CA", "90012"),
     ("123 N Main St #200", None, "Los Angeles", "California", "90012-1234"),
     ("123 N MAIN ST STE 200", "", "los angeles", "ca", "90012")],
    # facility name on line 1, street on line 2
    [("Kaiser Permanente Medical Offices", "4867 Sunset Boulevard", "Los Angeles", "CA", "90027"),
     ("4867 SUNSET BLVD", None, "LOS ANGELES", "CA", "90027")],
    # suite on line 1, street on line 2
    [("Suite 300", "1000 W. Carson St.", "Torrance", "CA", "90502"),
     ("1000 WEST CARSON STREET", "STE. 300", "TORRANCE", "CA", "90502")],
    # house number spelled out
    [("One Medical Plaza Drive", None, "Roseville", "CA", "95661"),
     ("1 MEDICAL PLAZA DR", None, "ROSEVILLE", "CA", "95661")],
    # ordinals and directional after the suffix
    [("200 First Avenue North", None, "X", "CA", "95000"),
     ("200 1ST AVE N", None, "X", "CA", "95000")],
    # real NPPES vs CMS Doctors & Clinicians near-misses (Oct 2026 files)
    [("24411 Health Center Dr", "Ste 350", "Laguna Hills", "CA", "92653"),
     ("24411 HEALTH CTR DR STE 350", None, "LAGUNA HILLS", "CA", "92653")],
    [("1317 Ynez Pl", "Ste A", "Coronado", "CA", "92118"),
     ("1317 YNEZ PL A", None, "CORONADO", "CA", "92118")],
    [("1524 McHenry Ave", "340", "Modesto", "CA", "95350"),
     ("1524 MCHENRY AVE 340", None, "MODESTO", "CA", "95350")],
    [("710 Lawrence Expressway", None, "Santa Clara", "CA", "95051"),
     ("710 LAWRENCE EXPRESS WAY", None, "SANTA CLARA", "CA", "95051")],
    [("1010 W La Veta Ave", "Ste 175", "Orange", "CA", "92868"),
     ("1010 W LAVETA AVE STE 175", None, "ORANGE", "CA", "92868")],
]


@pytest.mark.parametrize("variants", SAME_PLACE)
def test_variants_share_building_key(variants):
    keys = {std(*v).building_key for v in variants}
    assert len(keys) == 1 and None not in keys


def test_suite_does_not_change_building_but_changes_unit_id():
    a = std("123 Main St", "Suite 100", "Fresno", "CA", "93721")
    b = std("123 Main St", "Suite 200", "Fresno", "CA", "93721")
    c = std("123 Main St", None, "Fresno", "CA", "93721")
    assert a.address_id == b.address_id == c.address_id
    assert len({a.address_unit_id, b.address_unit_id, c.address_unit_id}) == 3
    assert std("123 Main St #200", None, "Fresno", "CA", "93721").address_unit_id == \
        std("123 Main St", "Suite 200", "Fresno", "CA", "93721").address_unit_id


def test_different_buildings_differ():
    a = std("123 Main St", None, "Fresno", "CA", "93721")
    assert a.address_id != std("125 Main St", None, "Fresno", "CA", "93721").address_id
    assert a.address_id != std("123 Main St", None, "Fresno", "CA", "93722").address_id


def test_display_format():
    r = std("123 North Main Street", "Suite 200", "Los Angeles", "CA", "900121234")
    assert (r.street, r.unit, r.zip5, r.zip4) == ("123 N MAIN ST", "STE 200", "90012", "1234")
    assert r.std_address == "123 N MAIN ST STE 200, LOS ANGELES, CA 90012"
    assert r.building_key == "123NMAINST|90012"


def test_readable_street_keeps_words_but_key_abbreviates():
    assert std("100 Buena Vista Street", None, "X", "CA", "95000").street == "100 BUENA VISTA ST"
    a = std("24411 Health Center Dr", None, "X", "CA", "92653")
    assert a.street == "24411 HEALTH CENTER DR"
    assert a.building_key == std("24411 Health Ctr Dr", None, "X", "CA", "92653").building_key


def test_street_names_that_look_like_units_are_kept():
    assert std("100 Tower Road", None, "X", "CA", "95000").street == "100 TOWER RD"
    assert std("100 Avenue A", None, "X", "CA", "95000").unit is None
    assert std("2000 Pacific Coast Hwy 1", None, "X", "CA", "95000").street == "2000 PACIFIC COAST HWY 1"
    assert std("100 Main St E", None, "X", "CA", "95000").street == "100 MAIN ST E"


def test_trailing_unit_and_floor_forms():
    r = std("1317 YNEZ PL A", None, "Coronado", "CA", "92118")
    assert r.street == "1317 YNEZ PL" and r.unit == "# A"
    assert std("500 Main St", "2nd Floor", "X", "CA", "95000").unit == \
        std("500 Main St Floor 2", None, "X", "CA", "95000").unit == "FL 2"


def test_facility_name_dropped():
    r = std("2100 Webster St", "California Pacific Medical Center", "San Francisco", "CA", "94115")
    assert r.unit is None and r.std_address == "2100 WEBSTER ST, SAN FRANCISCO, CA 94115"


@pytest.mark.parametrize("line1", ["PO Box 1234", "P.O. Box 1234", "P O BOX 1234", "Post Office Box 1234", "POB 1234"])
def test_po_box(line1):
    r = std(line1, None, "Sacramento", "CA", "95812")
    assert r.is_po_box and r.street == "PO BOX 1234"


def test_city_and_unicode():
    assert std("1 A St", None, "North Hollywood", "CA", None).city == "N HOLLYWOOD"
    r = std("1234 Peña Blvd.,", "Ste. #5", "San José", "CA", "95112")
    assert (r.street, r.unit, r.city) == ("1234 PENA BLVD", "STE 5", "SAN JOSE")


def test_no_zip_falls_back_to_city_state_and_nan_is_blank():
    a = std("123 Main St", None, "Fresno", "CA", None)
    assert a.building_key == std("123 Main St", math.nan, "Fresno", "CA", math.nan).building_key == "123MAINST|FRESNO|CA"
    assert a.building_key != std("123 Main St", None, "Clovis", "CA", None).building_key


def test_empty_input():
    r = std(None, None, None, None, None)
    assert r.building_key is None and r.std_address is None and not r.is_po_box
    assert building_key(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("90012", ("90012", None)), ("900121234", ("90012", "1234")), ("90012-1234", ("90012", "1234")),
    (90012.0, ("90012", None)), ("2134", ("02134", None)), ("900120000", ("90012", None)),
    ("00000", (None, None)), ("123", (None, None)), (None, (None, None)), (math.nan, (None, None)),
])
def test_normalize_zip(raw, expected):
    assert normalize_zip(raw) == expected
