from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings


LARGE_MARKETS = {
    "Argentina", "Australia", "Bangladesh", "Brazil", "Canada", "China", "Egypt", "France",
    "Germany", "India", "Indonesia", "Iran", "Italy", "Japan", "Mexico", "Nigeria", "Pakistan",
    "Philippines", "Poland", "Russia", "Saudi Arabia", "South Africa", "South Korea", "Spain",
    "Thailand", "Turkey", "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Vietnam",
}

MICRO_MARKETS = {
    "Andorra", "Antigua and Barbuda", "Bahamas", "Barbados", "Belize", "Bhutan", "Brunei",
    "Cabo Verde", "Comoros", "Dominica", "Equatorial Guinea", "Eswatini", "Fiji", "Grenada",
    "Iceland", "Kiribati", "Liechtenstein", "Luxembourg", "Maldives", "Malta", "Marshall Islands",
    "Micronesia", "Monaco", "Montenegro", "Nauru", "Palau", "Saint Kitts and Nevis", "Saint Lucia",
    "Saint Vincent and the Grenadines", "San Marino", "Sao Tome and Principe", "Seychelles",
    "Solomon Islands", "Timor-Leste", "Tonga", "Tuvalu", "Vanuatu", "Vatican City",
}

SMALL_MARKETS = {
    "Albania", "Armenia", "Bahrain", "Bosnia and Herzegovina", "Botswana", "Cyprus", "Estonia",
    "Georgia", "Guyana", "Jamaica", "Kosovo", "Kuwait", "Kyrgyzstan", "Latvia", "Lebanon",
    "Lithuania", "Mauritius", "Moldova", "Mongolia", "Namibia", "North Macedonia", "Oman",
    "Palestine", "Paraguay", "Qatar", "Slovakia", "Slovenia", "Suriname", "Trinidad and Tobago",
    "Turkmenistan", "Uruguay",
}

# Used only to diversify expansion-round text searches. Missing countries safely fall back to country-level searches.
COUNTRY_MAJOR_CITIES: dict[str, list[str]] = {
    "Afghanistan": ["Kabul", "Herat", "Kandahar"], "Albania": ["Tirana", "Durres"],
    "Algeria": ["Algiers", "Oran", "Constantine"], "Andorra": ["Andorra la Vella"],
    "Angola": ["Luanda", "Lobito"], "Argentina": ["Buenos Aires", "Cordoba", "Rosario"],
    "Armenia": ["Yerevan", "Gyumri"], "Australia": ["Sydney", "Melbourne", "Brisbane"],
    "Austria": ["Vienna", "Linz", "Graz"], "Azerbaijan": ["Baku", "Ganja"],
    "Bahrain": ["Manama"], "Bangladesh": ["Dhaka", "Chattogram", "Khulna"],
    "Belarus": ["Minsk", "Gomel"], "Belgium": ["Brussels", "Antwerp", "Liege"],
    "Bolivia": ["Santa Cruz", "La Paz", "Cochabamba"], "Bosnia and Herzegovina": ["Sarajevo", "Banja Luka"],
    "Botswana": ["Gaborone", "Francistown"], "Brazil": ["Sao Paulo", "Rio de Janeiro", "Belo Horizonte"],
    "Brunei": ["Bandar Seri Begawan"], "Bulgaria": ["Sofia", "Plovdiv", "Varna"],
    "Cambodia": ["Phnom Penh", "Siem Reap"], "Cameroon": ["Douala", "Yaounde"],
    "Canada": ["Toronto", "Montreal", "Vancouver"], "Chile": ["Santiago", "Concepcion", "Antofagasta"],
    "Colombia": ["Bogota", "Medellin", "Barranquilla"], "Costa Rica": ["San Jose", "Alajuela"],
    "Croatia": ["Zagreb", "Split", "Rijeka"], "Cyprus": ["Nicosia", "Limassol"],
    "Czech Republic": ["Prague", "Brno", "Ostrava"], "Denmark": ["Copenhagen", "Aarhus", "Odense"],
    "Dominican Republic": ["Santo Domingo", "Santiago"], "Ecuador": ["Quito", "Guayaquil", "Cuenca"],
    "Egypt": ["Cairo", "Alexandria", "Suez"], "El Salvador": ["San Salvador", "Santa Ana"],
    "Estonia": ["Tallinn", "Tartu"], "Finland": ["Helsinki", "Tampere", "Turku"],
    "France": ["Paris", "Lyon", "Marseille"], "Georgia": ["Tbilisi", "Batumi", "Kutaisi"],
    "Germany": ["Berlin", "Hamburg", "Dusseldorf"], "Ghana": ["Accra", "Kumasi", "Tema"],
    "Greece": ["Athens", "Piraeus", "Thessaloniki"], "Guatemala": ["Guatemala City", "Quetzaltenango"],
    "Hungary": ["Budapest", "Debrecen", "Gyor"], "Iceland": ["Reykjavik"],
    "India": ["Mumbai", "Delhi", "Ahmedabad"], "Indonesia": ["Jakarta", "Surabaya", "Bandung"],
    "Iran": ["Tehran", "Isfahan", "Mashhad"], "Iraq": ["Baghdad", "Basra", "Erbil"],
    "Ireland": ["Dublin", "Cork", "Limerick"], "Israel": ["Tel Aviv", "Haifa", "Jerusalem"],
    "Italy": ["Milan", "Rome", "Turin"], "Japan": ["Tokyo", "Osaka", "Nagoya"],
    "Jordan": ["Amman", "Zarqa", "Aqaba"], "Kazakhstan": ["Almaty", "Astana", "Karaganda"],
    "Kenya": ["Nairobi", "Mombasa", "Kisumu"], "Kosovo": ["Pristina", "Prizren"],
    "Kuwait": ["Kuwait City", "Al Ahmadi"], "Kyrgyzstan": ["Bishkek", "Osh"],
    "Laos": ["Vientiane", "Savannakhet"], "Latvia": ["Riga", "Daugavpils"],
    "Lebanon": ["Beirut", "Tripoli"], "Lithuania": ["Vilnius", "Kaunas", "Klaipeda"],
    "Luxembourg": ["Luxembourg City"], "Malaysia": ["Kuala Lumpur", "Johor Bahru", "Penang"],
    "Malta": ["Valletta", "Birkirkara"], "Mexico": ["Mexico City", "Monterrey", "Guadalajara"],
    "Moldova": ["Chisinau", "Balti"], "Monaco": ["Monaco"],
    "Mongolia": ["Ulaanbaatar", "Erdenet"], "Montenegro": ["Podgorica", "Bar"],
    "Morocco": ["Casablanca", "Tangier", "Rabat"], "Myanmar": ["Yangon", "Mandalay"],
    "Namibia": ["Windhoek", "Walvis Bay"], "Nepal": ["Kathmandu", "Biratnagar"],
    "Netherlands": ["Amsterdam", "Rotterdam", "Eindhoven"], "New Zealand": ["Auckland", "Christchurch", "Wellington"],
    "Nigeria": ["Lagos", "Abuja", "Port Harcourt"], "North Macedonia": ["Skopje", "Bitola"],
    "Norway": ["Oslo", "Bergen", "Stavanger"], "Oman": ["Muscat", "Sohar", "Salalah"],
    "Pakistan": ["Karachi", "Lahore", "Faisalabad"], "Palestine": ["Ramallah", "Nablus"],
    "Panama": ["Panama City", "Colon"], "Paraguay": ["Asuncion", "Ciudad del Este"],
    "Peru": ["Lima", "Arequipa", "Trujillo"], "Philippines": ["Manila", "Cebu", "Davao"],
    "Poland": ["Warsaw", "Katowice", "Gdansk"], "Portugal": ["Lisbon", "Porto", "Braga"],
    "Qatar": ["Doha", "Al Rayyan"], "Romania": ["Bucharest", "Cluj-Napoca", "Constanta"],
    "Russia": ["Moscow", "Saint Petersburg", "Yekaterinburg"], "Saudi Arabia": ["Riyadh", "Jeddah", "Dammam"],
    "Serbia": ["Belgrade", "Novi Sad", "Nis"], "Singapore": ["Singapore"],
    "Slovakia": ["Bratislava", "Kosice"], "Slovenia": ["Ljubljana", "Maribor", "Koper"],
    "South Africa": ["Johannesburg", "Cape Town", "Durban"], "South Korea": ["Seoul", "Busan", "Incheon"],
    "Spain": ["Madrid", "Barcelona", "Bilbao"], "Sri Lanka": ["Colombo", "Kandy"],
    "Sweden": ["Stockholm", "Gothenburg", "Malmo"], "Switzerland": ["Zurich", "Geneva", "Basel"],
    "Syria": ["Damascus", "Aleppo"], "Taiwan": ["Taipei", "Kaohsiung", "Taichung"],
    "Tajikistan": ["Dushanbe", "Khujand"], "Tanzania": ["Dar es Salaam", "Mwanza", "Arusha"],
    "Thailand": ["Bangkok", "Chonburi", "Rayong"], "Trinidad and Tobago": ["Port of Spain", "San Fernando"],
    "Tunisia": ["Tunis", "Sfax"], "Turkey": ["Istanbul", "Ankara", "Izmir"],
    "Turkmenistan": ["Ashgabat", "Turkmenbashi"], "Ukraine": ["Kyiv", "Dnipro", "Odesa"],
    "United Arab Emirates": ["Dubai", "Abu Dhabi", "Sharjah"], "United Kingdom": ["London", "Birmingham", "Manchester"],
    "United States": ["Houston", "Chicago", "Los Angeles"], "Uruguay": ["Montevideo", "Salto"],
    "Uzbekistan": ["Tashkent", "Samarkand"], "Venezuela": ["Caracas", "Maracaibo", "Valencia"],
    "Vietnam": ["Ho Chi Minh City", "Hanoi", "Hai Phong"], "Yemen": ["Sanaa", "Aden"],
    "Zambia": ["Lusaka", "Kitwe"], "Zimbabwe": ["Harare", "Bulawayo"],
}

COUNTRY_MAJOR_CITIES.update(
    {
        "Antigua and Barbuda": ["Saint John's"], "Bahamas": ["Nassau", "Freeport"],
        "Barbados": ["Bridgetown"], "Belize": ["Belize City", "Belmopan"],
        "Bhutan": ["Thimphu", "Phuentsholing"], "Cuba": ["Havana", "Santiago de Cuba"],
        "Dominica": ["Roseau"], "Fiji": ["Suva", "Lautoka"], "Grenada": ["Saint George's"],
        "Guyana": ["Georgetown", "Linden"], "Haiti": ["Port-au-Prince", "Cap-Haitien"],
        "Honduras": ["Tegucigalpa", "San Pedro Sula"], "Jamaica": ["Kingston", "Montego Bay"],
        "Kiribati": ["South Tarawa"], "Liechtenstein": ["Vaduz", "Schaan"],
        "Maldives": ["Male"], "Marshall Islands": ["Majuro"],
        "Micronesia": ["Palikir", "Weno"], "Nauru": ["Yaren"],
        "Nicaragua": ["Managua", "Leon"], "Palau": ["Koror"],
        "Papua New Guinea": ["Port Moresby", "Lae"], "Saint Kitts and Nevis": ["Basseterre"],
        "Saint Lucia": ["Castries"], "Saint Vincent and the Grenadines": ["Kingstown"],
        "Samoa": ["Apia"], "San Marino": ["San Marino"], "Solomon Islands": ["Honiara"],
        "Suriname": ["Paramaribo"], "Timor-Leste": ["Dili"], "Tonga": ["Nuku'alofa"],
        "Tuvalu": ["Funafuti"], "Vanuatu": ["Port Vila", "Luganville"],
        "Vatican City": ["Vatican City"],
    }
)


@dataclass(frozen=True)
class ScheduledCountry:
    country: str
    round_no: int
    phase: str
    target_count: int
    lead_count: int
    query_limit: int


@dataclass(frozen=True)
class SchedulePlan:
    countries: list[ScheduledCountry]
    cooldown_skips: list[tuple[str, int, int, int]]
    next_cooldown_at: str


def country_size_tier(country: str) -> str:
    if country in LARGE_MARKETS:
        return "large"
    if country in MICRO_MARKETS:
        return "micro"
    if country in SMALL_MARKETS:
        return "small"
    return "medium"


def country_target(settings: Settings, country: str) -> int:
    tier = country_size_tier(country)
    return {
        "large": settings.discovery_target_large,
        "medium": settings.discovery_target_medium,
        "small": settings.discovery_target_small,
        "micro": settings.discovery_target_micro,
    }[tier]


def plan_country_schedule(
    conn: sqlite3.Connection,
    settings: Settings,
    countries: list[str],
    limit: int,
    base_query_limit: int,
) -> SchedulePlan:
    if not countries or limit <= 0:
        return SchedulePlan([], [], "")
    status = country_discovery_status(conn, settings, countries)
    order = {country: index for index, country in enumerate(countries)}
    active_cooldowns = {country for country, item in status.items() if item["cooldown_active"]}
    effective_rounds = {
        country: item["rounds_completed"]
        + (1 if country in active_cooldowns and not item["cooldown_skip_recorded"] else 0)
        for country, item in status.items()
    }
    minimum_round = min(effective_rounds.values(), default=0)
    next_round = minimum_round + 1
    candidates = [
        country
        for country in countries
        if country not in active_cooldowns and effective_rounds[country] < next_round
    ]
    candidates.sort(
        key=lambda country: (
            status[country]["lead_count"] >= status[country]["target_count"],
            status[country]["lead_count"] / max(status[country]["target_count"], 1),
            status[country]["last_searched_at"] or "",
            order[country],
        )
    )
    selected = []
    for country in candidates[:limit]:
        item = status[country]
        selected.append(
            ScheduledCountry(
                country=country,
                round_no=item["rounds_completed"] + 1,
                phase="baseline" if item["rounds_completed"] == 0 else "expansion",
                target_count=item["target_count"],
                lead_count=item["lead_count"],
                query_limit=max(base_query_limit, 1) if item["lead_count"] < item["target_count"] else 1,
            )
        )
    skips = []
    for country in active_cooldowns:
        item = status[country]
        if not item["cooldown_skip_recorded"] and item["rounds_completed"] + 1 == next_round:
            skips.append((country, next_round, item["target_count"], item["lead_count"]))
    next_cooldown = min(
        (status[country]["cooldown_until"] for country in active_cooldowns if status[country]["cooldown_until"]),
        default="",
    )
    return SchedulePlan(selected, skips, next_cooldown)


def scheduled_queries(
    conn: sqlite3.Connection,
    query_specs: list[tuple[str, str]],
    scheduled: ScheduledCountry,
    settings: Settings | None = None,
) -> list[dict]:
    country_lower = scheduled.country.lower()
    global_queries = [query for query, country in query_specs if not country]
    local_queries = [query for query, country in query_specs if country.lower() == country_lower]
    used = {
        row["query"]
        for row in conn.execute(
            """
            SELECT DISTINCT query FROM discovery_runs
            WHERE lower(country) = lower(?) AND COALESCE(error, '') = ''
            """,
            (scheduled.country,),
        )
    }
    rows: list[dict] = []
    if scheduled.phase == "baseline":
        for base_query in global_queries:
            query = f"{base_query} {scheduled.country}"
            if query not in used:
                rows.append(query_row(scheduled, base_query, "", query))
            if len([row for row in rows if not row["is_local"]]) >= scheduled.query_limit:
                break
        for base_query in local_queries:
            query = f"{base_query} {scheduled.country}"
            if query not in used:
                item = query_row(scheduled, base_query, "", query)
                item["is_local"] = True
                rows.append(item)
        return rows

    cities = major_cities(settings, scheduled.country)
    preferred_city = cities[(scheduled.round_no - 2) % len(cities)] if cities else ""
    city_order = ([preferred_city] + [city for city in cities if city != preferred_city]) if cities else [""]
    start = ((scheduled.round_no - 1) * scheduled.query_limit) % max(len(global_queries), 1)
    rotated_globals = global_queries[start:] + global_queries[:start]
    global_candidates = []
    for city in city_order:
        for base_query in rotated_globals:
            query = " ".join(part for part in [base_query, city, scheduled.country] if part)
            if query not in used:
                global_candidates.append(query_row(scheduled, base_query, city, query))
    rows.extend(global_candidates[: scheduled.query_limit])
    for base_query in local_queries:
        for city in city_order:
            query = " ".join(part for part in [base_query, city, scheduled.country] if part)
            if query not in used:
                item = query_row(scheduled, base_query, city, query)
                item["is_local"] = True
                rows.append(item)
                break
    return rows


def major_cities(settings: Settings | None, country: str) -> list[str]:
    cities = COUNTRY_MAJOR_CITIES.get(country, [])
    if not settings or not settings.discovery_cities_file:
        return cities
    path = Path(settings.discovery_cities_file)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cities
    custom = payload.get(country) if isinstance(payload, dict) else None
    if not isinstance(custom, list):
        return cities
    clean = [str(city).strip() for city in custom if str(city).strip()]
    return clean or cities


def query_row(scheduled: ScheduledCountry, base_query: str, city: str, query: str) -> dict:
    return {
        "country": scheduled.country,
        "round_no": scheduled.round_no,
        "phase": scheduled.phase,
        "target_count": scheduled.target_count,
        "lead_count_before": scheduled.lead_count,
        "base_query": base_query,
        "city": city,
        "query": query,
        "is_local": False,
    }


def record_discovery_run(
    conn: sqlite3.Connection,
    *,
    country: str,
    query: str,
    base_query: str = "",
    city: str = "",
    round_no: int = 0,
    phase: str = "manual",
    result_count: int = 0,
    unique_count: int = 0,
    new_count: int = 0,
    error: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO discovery_runs (
            country, query, base_query, city, round_no, phase,
            result_count, unique_website_count, new_website_count, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (country, query, base_query, city, round_no, phase, result_count, unique_count, new_count, error[:500]),
    )


def record_country_round(
    conn: sqlite3.Connection,
    scheduled: ScheduledCountry,
    *,
    query_count: int,
    result_count: int,
    new_count: int,
    failed_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO discovery_country_rounds (
            country, round_no, target_count, lead_count_before, query_count,
            result_count, new_website_count, failed_query_count, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed')
        ON CONFLICT(country, round_no) DO UPDATE SET
            target_count = excluded.target_count,
            lead_count_before = excluded.lead_count_before,
            query_count = excluded.query_count,
            result_count = excluded.result_count,
            new_website_count = excluded.new_website_count,
            failed_query_count = excluded.failed_query_count,
            status = 'completed',
            completed_at = CURRENT_TIMESTAMP
        """,
        (
            scheduled.country,
            scheduled.round_no,
            scheduled.target_count,
            scheduled.lead_count,
            query_count,
            result_count,
            new_count,
            failed_count,
        ),
    )


def record_cooldown_skips(conn: sqlite3.Connection, skips: list[tuple[str, int, int, int]]) -> None:
    for country, round_no, target_count, lead_count in skips:
        conn.execute(
            """
            INSERT OR IGNORE INTO discovery_country_rounds (
                country, round_no, target_count, lead_count_before, status
            ) VALUES (?, ?, ?, ?, 'cooldown_skipped')
            """,
            (country, round_no, target_count, lead_count),
        )


def country_discovery_status(
    conn: sqlite3.Connection,
    settings: Settings,
    countries: list[str],
) -> dict[str, dict]:
    lead_counts = {
        (row["country"] or "").strip().lower(): int(row["count"])
        for row in conn.execute(
            "SELECT country, COUNT(*) AS count FROM leads WHERE country IS NOT NULL AND country != '' GROUP BY country"
        )
    }
    round_rows = conn.execute(
        """
        SELECT country, round_no, query_count, new_website_count, failed_query_count, status, completed_at
        FROM discovery_country_rounds
        ORDER BY country, round_no DESC
        """
    ).fetchall()
    rounds_by_country: dict[str, list[sqlite3.Row]] = {}
    for row in round_rows:
        rounds_by_country.setdefault(row["country"].lower(), []).append(row)
    last_runs = {
        row["country"].lower(): row["last_searched_at"]
        for row in conn.execute(
            "SELECT country, MAX(searched_at) AS last_searched_at FROM discovery_runs GROUP BY country"
        )
    }
    now = datetime.now(timezone.utc)
    status = {}
    for country in countries:
        key = country.lower()
        lead_count = lead_counts.get(key, 0)
        rows = rounds_by_country.get(key, [])
        rounds_completed = max((int(row["round_no"]) for row in rows), default=0)
        legacy = rounds_completed == 0 and lead_count > 0
        if legacy:
            rounds_completed = 1
        completed_rows = [row for row in rows if row["status"] == "completed"]
        cooldown_skip_recorded = bool(rows and rows[0]["status"] == "cooldown_skipped")
        no_new_rounds = 0
        for row in completed_rows:
            if int(row["query_count"] or 0) <= int(row["failed_query_count"] or 0):
                break
            if int(row["new_website_count"] or 0) > 0:
                break
            no_new_rounds += 1
        cooldown_until = ""
        cooldown_active = False
        if no_new_rounds >= max(settings.discovery_no_new_cooldown_rounds, 1) and completed_rows:
            completed_at = parse_utc(completed_rows[0]["completed_at"])
            if completed_at:
                until = completed_at + timedelta(hours=max(settings.discovery_cooldown_hours, 0))
                cooldown_until = until.strftime("%Y-%m-%d %H:%M:%S")
                cooldown_active = until > now
        status[country] = {
            "country": country,
            "tier": country_size_tier(country),
            "target_count": country_target(settings, country),
            "lead_count": lead_count,
            "rounds_completed": rounds_completed,
            "legacy_bootstrap": legacy,
            "no_new_rounds": no_new_rounds,
            "cooldown_active": cooldown_active,
            "cooldown_skip_recorded": cooldown_skip_recorded,
            "cooldown_until": cooldown_until,
            "last_searched_at": last_runs.get(key, ""),
        }
    return status


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
