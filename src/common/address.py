"""Shared address standardization (C5): one rule set used everywhere an address is compared.

Goal: a canonical *matching* key that is identical when two sources describe the same building, not USPS
certification. Rules are a subset of USPS Publication 28 (street suffixes, directionals, secondary unit
designators). Deterministic, no network: the Census geocoder (stage5_address_map) then validates the result.

Every rule was added because real NPPES vs CMS Doctors & Clinicians pairs disagreed on it
(docs/C5_logic_TIN_NPI_address_and_specialty.md, section "Address standardization"):
  * CMS abbreviates every word ("HEALTH CTR DR"), NPPES often doesn't ("HEALTH CENTER DR")
  * suites written without a designator ("1317 YNEZ PL A" = "1317 YNEZ PL STE A", "MCHENRY AVE 340")
  * spacing variants ("LA VETA" = "LAVETA", "EXPRESS WAY" = "EXPRESSWAY")
  * house numbers spelled out ("ONE MEDICAL PLAZA DR" = "1 MEDICAL PLZ DR")
  * facility name on line 1 and street on line 2 (swapped), PO boxes (flagged: never a practice location)

Keys
  building_key   street key + ZIP5 (city + state if no ZIP). Suites are ignored: sources disagree on the
                 suite far more often than on the street, so agreement is measured per building.
  address_id     short hash of building_key; address_unit_id adds the suite values (display de-duplication).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

STREET_SUFFIXES = {
    "ALLEY": "ALY", "ALLY": "ALY", "AVENUE": "AVE", "AV": "AVE", "AVEN": "AVE", "AVN": "AVE",
    "BOULEVARD": "BLVD", "BOULV": "BLVD", "BOUL": "BLVD", "CENTER": "CTR", "CENTRE": "CTR",
    "CIRCLE": "CIR", "CIRC": "CIR", "COURT": "CT", "CRT": "CT", "COVE": "CV", "CREEK": "CRK",
    "CROSSING": "XING", "DRIVE": "DR", "DRV": "DR", "EXPRESSWAY": "EXPY", "EXPRESS": "EXPY",
    "FREEWAY": "FWY", "FRWY": "FWY", "GROVE": "GRV", "HEIGHTS": "HTS", "HIGHWAY": "HWY",
    "HIWAY": "HWY", "HILL": "HL", "HILLS": "HLS", "JUNCTION": "JCT", "LANE": "LN",
    "LOOP": "LOOP", "MALL": "MALL", "PARK": "PARK", "PARKWAY": "PKWY", "PKY": "PKWY",
    "PARKWY": "PKWY", "PLACE": "PL", "PLAZA": "PLZ", "PLZA": "PLZ", "POINT": "PT",
    "ROAD": "RD", "ROUTE": "RTE", "SQUARE": "SQ", "SQR": "SQ", "STREET": "ST", "STR": "ST",
    "TERRACE": "TER", "TERR": "TER", "TRAIL": "TRL", "TRAILS": "TRL", "TURNPIKE": "TPKE",
    "VALLEY": "VLY", "VIEW": "VW", "VILLAGE": "VLG", "VISTA": "VIS", "WAY": "WAY",
    "WY": "WAY",
}
DIRECTIONALS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
COMMON_WORDS = {"SAINT": "ST", "MOUNT": "MT", "FORT": "FT"}   # appear in street and city names
ORDINAL_WORDS = {
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
    "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH",
}
NUMBER_WORDS = {
    "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
    "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10",
}
# Words that are also common street names (TOWER, PARK, WING) are deliberately absent.
UNIT_DESIGNATORS = {
    "SUITE": "STE", "STE": "STE", "#": "#",
    "APARTMENT": "APT", "APT": "APT", "UNIT": "UNIT", "ROOM": "RM", "RM": "RM",
    "BUILDING": "BLDG", "BLDG": "BLDG", "BLD": "BLDG", "FLOOR": "FL", "FL": "FL",
    "FLR": "FL", "SPC": "SPC", "DEPARTMENT": "DEPT", "DEPT": "DEPT",
}
STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "PUERTO RICO": "PR", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

_PO_BOX = re.compile(r"^(?:POST\s+OFFICE|P\s?O\s?B|P\s?O)\b\s*(?:BOX|BX)?\s*(?=\w)|^(?:BOX|BX)\s+(?=\d)")
_DESIGNATOR_HASH = re.compile(
    r"\b(SUITE|STE|UNIT|APARTMENT|APT|ROOM|RM|BUILDING|BLDG|FLOOR|FLR|FL|SPC|DEPT)\s+#\s*")
_ORDINAL_FLOOR = re.compile(r"\b(\d+)(?:ST|ND|RD|TH)\s+(?:FL|FLR|FLOOR)\b")
_GLUED_UNIT = re.compile(r"\b(STE|SUITE|UNIT|APT|RM|BLDG)(\d+[A-Z]?)\b")
_UNIT_ONLY = re.compile(r"^(?:[A-Z]?\d+[A-Z]?(?:-[A-Z0-9]+)?|[A-Z])$")
_SPLIT_WORDS = re.compile(r"\b(EXPRESS|FREE|PARK)\s+WAY\b")
# A bare trailing token after the suffix is a unit ("YNEZ PL A", "MCHENRY AVE 340"),
# except after route-type suffixes where it is the route number ("HWY 1").
_TRAILING_UNIT = re.compile(r"^(?:\d+[A-Z]?|[A-Z]\d*|\d+-[A-Z0-9]+)$")
_SUFFIX_ABBRS = set(STREET_SUFFIXES.values())
_ROUTE_SUFFIXES = {"HWY", "RTE", "FWY", "EXPY", "TPKE"}


@dataclass(frozen=True)
class StdAddress:
    street: str | None        # "123 N MAIN ST" (readable; suffix abbreviated in suffix position)
    unit: str | None          # "STE 200", "FL 2", "# A"
    city: str | None
    state: str | None
    zip5: str | None
    zip4: str | None
    is_po_box: bool
    std_address: str | None   # "123 N MAIN ST STE 200, LOS ANGELES, CA 90012"
    building_key: str | None  # "123NMAINST|90012"
    address_id: str | None
    address_unit_id: str | None


def _blank(x) -> bool:
    return x is None or (isinstance(x, float) and x != x)   # None or NaN (pandas)


def _clean(text) -> str:
    """Upper-case ASCII, punctuation stripped, whitespace collapsed."""
    if _blank(text):
        return ""
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = s.upper().replace(".", "").replace("'", "").replace("&", " AND ")
    s = re.sub(r"#", " # ", s)
    s = re.sub(r"[^A-Z0-9#/\- ]", " ", s)
    s = _DESIGNATOR_HASH.sub(r"\1 ", s)          # "STE #5" -> "STE 5"
    s = re.sub(r"\s*-\s*", "-", s)
    s = _SPLIT_WORDS.sub(r"\1WAY", s)            # "EXPRESS WAY" -> "EXPRESSWAY"
    s = _ORDINAL_FLOOR.sub(r"FL \1", s)          # "2ND FLOOR" -> "FL 2"
    s = _GLUED_UNIT.sub(r"\1 \2", s)             # "STE200" -> "STE 200"
    return " ".join(s.split())


def _normalize_po_box(s: str) -> str:
    return _PO_BOX.sub("PO BOX ", s).strip() if _PO_BOX.match(s) else s


def _looks_like_street(s: str) -> bool:
    return bool(s) and (s[0].isdigit() or s.split(" ", 1)[0] in NUMBER_WORDS or s.startswith("PO BOX"))


def _is_unit_text(s: str) -> bool:
    return bool(s) and (s.split(" ", 1)[0] in UNIT_DESIGNATORS or bool(_UNIT_ONLY.match(s)))


def _parse_unit_tokens(tokens: list[str]) -> list[tuple[str, str]]:
    units, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in UNIT_DESIGNATORS and i + 1 < len(tokens) and tokens[i + 1] not in UNIT_DESIGNATORS:
            units.append((UNIT_DESIGNATORS[tok], tokens[i + 1]))
            i += 2
        elif tok not in UNIT_DESIGNATORS:
            units.append(("#", tok))   # bare value, e.g. line 2 = "200"
            i += 1
        else:
            i += 1                      # designator with no value
    return units


def _split_units(tokens: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Street tokens vs (designator, value) unit pairs. A designator at position 0 is never a unit."""
    for i, tok in enumerate(tokens):
        if i > 0 and tok in UNIT_DESIGNATORS:
            return tokens[:i], _parse_unit_tokens(tokens[i:])
    return tokens, []


def _pop_trailing_unit(tokens: list[str]) -> tuple[list[str], str | None]:
    if len(tokens) < 4:   # number + name + suffix + unit; keeps "100 AVE A"
        return tokens, None
    last, prev = tokens[-1], STREET_SUFFIXES.get(tokens[-2], tokens[-2])
    if (_TRAILING_UNIT.match(last) and last not in DIRECTIONALS.values()
            and prev in _SUFFIX_ABBRS and prev not in _ROUTE_SUFFIXES):
        return tokens[:-1], last
    return tokens, None


def _normalize_street(tokens: list[str]) -> list[str]:
    """Number words -> digits; directionals / ordinals / SAINT after the house number; the suffix only in
    suffix position (so "BUENA VISTA ST" keeps VISTA in the readable form)."""
    out = [NUMBER_WORDS.get(tokens[0], tokens[0])] if tokens else []
    out += [DIRECTIONALS.get(t) or ORDINAL_WORDS.get(t) or COMMON_WORDS.get(t) or t for t in tokens[1:]]
    pos = len(out) - 2 if len(out) >= 3 and out[-1] in DIRECTIONALS.values() else len(out) - 1
    if pos >= 2 or (pos == 1 and not out[0][:1].isdigit()):
        out[pos] = STREET_SUFFIXES.get(out[pos], out[pos])
    return out


def _street_key(street: str) -> str:
    """Matching form: every word abbreviated and spaces removed ("HEALTH CENTER DR" = "HEALTHCTRDR")."""
    return "".join(STREET_SUFFIXES.get(t, t) for t in street.split())


def _normalize_city(city) -> str | None:
    tokens = [DIRECTIONALS.get(t) or COMMON_WORDS.get(t) or t for t in _clean(city).replace("#", "").split()]
    return " ".join(tokens) or None


def _normalize_state(state) -> str | None:
    s = _clean(state)
    return (STATE_NAMES.get(s, s) if len(s) > 2 else s) or None


def normalize_zip(zip_code) -> tuple[str | None, str | None]:
    """(zip5, zip4). Restores a leading zero lost by numeric conversion."""
    if _blank(zip_code):
        return None, None
    s = str(zip_code).strip()
    if s.endswith(".0"):
        s = s[:-2]
    d = re.sub(r"\D", "", s)
    if len(d) in (4, 8):
        d = "0" + d
    if len(d) == 9:
        zip5, zip4 = d[:5], d[5:]
    elif len(d) == 5:
        zip5, zip4 = d, None
    else:
        return None, None
    if zip5 == "00000":
        return None, None
    return zip5, (zip4 if zip4 and zip4 != "0000" else None)


def _hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def standardize_address(line1=None, line2=None, city=None, state=None, zip_code=None) -> StdAddress:
    l1, l2 = _normalize_po_box(_clean(line1)), _normalize_po_box(_clean(line2))
    if not _looks_like_street(l1) and _looks_like_street(l2):   # facility name on line 1, street on line 2
        l1, l2 = l2, l1
    if not l1 and l2:
        l1, l2 = l2, ""

    street_tokens, units = _split_units(l1.split())
    if not units and street_tokens[:1] != ["PO"]:
        street_tokens, trailing = _pop_trailing_unit(street_tokens)
        if trailing:
            units.append(("#", trailing))
    if l2 and _is_unit_text(l2):          # line 2 is a suite; otherwise a facility name, dropped from the key
        units += _parse_unit_tokens(l2.split())

    street = " ".join(_normalize_street(street_tokens)) or None
    unit = " ".join(f"{d} {v}" for d, v in units) or None
    city_n, state_n = _normalize_city(city), _normalize_state(state)
    zip5, zip4 = normalize_zip(zip_code)
    is_po_box = bool(street and street.startswith("PO BOX"))

    building_key = address_id = address_unit_id = std_address = None
    if street:
        loc = zip5 or (city_n or "") + "|" + (state_n or "")
        building_key = _street_key(street) + "|" + loc
        address_id = _hash(building_key)
        address_unit_id = _hash(building_key + "|" + "|".join(v for _, v in units))
        tail = " ".join(p for p in (state_n, zip5) if p)
        std_address = ", ".join(p for p in (f"{street} {unit}" if unit else street, city_n, tail) if p)
    return StdAddress(street, unit, city_n, state_n, zip5, zip4, is_po_box, std_address,
                      building_key, address_id, address_unit_id)


def building_key(line1, city=None, state=None, zip_code=None, line2=None) -> str:
    """Building identity used to compare addresses across sources ('' if there is no street)."""
    return standardize_address(line1, line2, city, state, zip_code).building_key or ""
