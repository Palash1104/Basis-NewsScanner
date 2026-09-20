"""Canonical country names for extracted events, so playbook rules can match them exactly.

The model is asked for standard English short names, but writes "US", "U.S.", "USA" or
"United States of America" interchangeably. `normalize_countries` maps known variants to one
canonical name and reports anything it can't map (kept as written, never dropped), so the run
output shows names that need an alias.

Canonical names: the 193 UN member states in common English short form, plus the two UN
observer states (Palestine, Vatican City) and Taiwan, Kosovo, Hong Kong and Greenland, which
appear in news as separate places.
"""

from collections.abc import Iterable

UN_MEMBERS = (
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda",
    "Argentina", "Armenia", "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain",
    "Bangladesh", "Barbados", "Belarus", "Belgium", "Belize", "Benin", "Bhutan", "Bolivia",
    "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria", "Burkina Faso",
    "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada", "Central African Republic",
    "Chad", "Chile", "China", "Colombia", "Comoros", "Republic of the Congo", "Costa Rica",
    "Côte d'Ivoire", "Croatia", "Cuba", "Cyprus", "Czech Republic",
    "Democratic Republic of the Congo", "Denmark", "Djibouti", "Dominica",
    "Dominican Republic", "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea", "Eritrea",
    "Estonia", "Eswatini", "Ethiopia", "Fiji", "Finland", "France", "Gabon", "Gambia",
    "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guatemala", "Guinea",
    "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hungary", "Iceland", "India",
    "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan", "Jordan",
    "Kazakhstan", "Kenya", "Kiribati", "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon",
    "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg", "Madagascar",
    "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania",
    "Mauritius", "Mexico", "Micronesia", "Moldova", "Monaco", "Mongolia", "Montenegro",
    "Morocco", "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "Netherlands",
    "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea", "North Macedonia",
    "Norway", "Oman", "Pakistan", "Palau", "Panama", "Papua New Guinea", "Paraguay", "Peru",
    "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa",
    "San Marino", "Sao Tome and Principe", "Saudi Arabia", "Senegal", "Serbia", "Seychelles",
    "Sierra Leone", "Singapore", "Slovakia", "Slovenia", "Solomon Islands", "Somalia",
    "South Africa", "South Korea", "South Sudan", "Spain", "Sri Lanka", "Sudan", "Suriname",
    "Sweden", "Switzerland", "Syria", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste",
    "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu",
    "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States",
    "Uruguay", "Uzbekistan", "Vanuatu", "Venezuela", "Vietnam", "Yemen", "Zambia",
    "Zimbabwe",
)  # fmt: skip
# Not UN member states, but they appear in news as distinct places.
OTHER_PLACES = ("Palestine", "Vatican City", "Taiwan", "Kosovo", "Hong Kong", "Greenland")
CANONICAL = frozenset(UN_MEMBERS + OTHER_PLACES)

# Other spellings -> canonical name. Keys are compared case-insensitively, after dropping a
# leading "the". Ambiguous names ("Korea", "Congo") are deliberately left unmapped.
ALIASES = {
    "us": "United States",
    "u.s.": "United States",
    "usa": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "russian federation": "Russia",
    "people's republic of china": "China",
    "prc": "China",
    "mainland china": "China",
    "republic of korea": "South Korea",
    "s. korea": "South Korea",
    "dprk": "North Korea",
    "democratic people's republic of korea": "North Korea",
    "islamic republic of iran": "Iran",
    "syrian arab republic": "Syria",
    "viet nam": "Vietnam",
    "lao pdr": "Laos",
    "czechia": "Czech Republic",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "ivory coast": "Côte d'Ivoire",
    "cote d'ivoire": "Côte d'Ivoire",
    "cape verde": "Cabo Verde",
    "swaziland": "Eswatini",
    "burma": "Myanmar",
    "east timor": "Timor-Leste",
    "macedonia": "North Macedonia",
    "holland": "Netherlands",
    "drc": "Democratic Republic of the Congo",
    "dr congo": "Democratic Republic of the Congo",
    "state of palestine": "Palestine",
    "palestinian territories": "Palestine",
    "gaza": "Palestine",
    "gaza strip": "Palestine",
    "west bank": "Palestine",
    "holy see": "Vatican City",
    "vatican": "Vatican City",
    "republic of china": "Taiwan",
    "hong kong sar": "Hong Kong",
}

_LOOKUP = {name.casefold(): name for name in CANONICAL} | ALIASES


def canonical_country(name: str) -> str | None:
    """The canonical name for `name`, or None if it isn't a known country or alias."""
    key = " ".join(name.split()).casefold()
    if key.startswith("the "):
        key = key[4:]
    return _LOOKUP.get(key)


def normalize_countries(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """(countries, unmapped): known names made canonical and de-duplicated in order; unknown
    names kept as written (so nothing is lost) and also returned in `unmapped`."""
    countries: list[str] = []
    unmapped: list[str] = []
    for name in names:
        canonical = canonical_country(name)
        value = canonical or " ".join(name.split())
        if canonical is None:
            unmapped.append(value)
        if value not in countries:
            countries.append(value)
    return countries, unmapped
