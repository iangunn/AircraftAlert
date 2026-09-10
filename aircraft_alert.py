# Standard library imports
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import logging
from math import atan2, cos, degrees, radians, sin, sqrt
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

# Third-party imports
import apprise
from dotenv import load_dotenv
import requests

# Load environment variables
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_directory = os.getenv('LOG_DIR', 'logs')
os.makedirs(log_directory, exist_ok=True)
log_file_path = os.path.join(log_directory, 'aircraft.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress apprise info messages (e.g. "Sent Pushover notification...")
logging.getLogger('apprise').setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# CSV alert log
# ---------------------------------------------------------------------------
csv_log_path = os.path.join(log_directory, 'alerts.csv')
CSV_FIELDS = ['date', 'time', 'icao24', 'registration', 'callsign', 'type_code', 'aircraft_type',
              'lat', 'lon', 'alt_baro', 'gs', 'track', 'military']


def _init_csv():
    """Create CSV with header row if it doesn't already exist."""
    if not os.path.exists(csv_log_path):
        with open(csv_log_path, 'w', newline='', encoding='utf-8') as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def log_alert_csv(aircraft: 'Aircraft'):
    """Append one alert row to the CSV log."""
    now = datetime.now()
    with open(csv_log_path, 'a', newline='', encoding='utf-8') as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow({
            'date':          now.strftime('%Y-%m-%d'),
            'time':          now.strftime('%H:%M:%S'),
            'icao24':        aircraft.icao24,
            'registration':  aircraft.registration,
            'callsign':      aircraft.callsign,
            'type_code':     aircraft.type_code,
            'aircraft_type': lookup_aircraft_type(aircraft.icao24) or '',
            'lat':           aircraft.latitude,
            'lon':           aircraft.longitude,
            'alt_baro':      aircraft.alt_baro if aircraft.alt_baro is not None else '',
            'gs':            aircraft.gs if aircraft.gs is not None else '',
            'track':         aircraft.track if aircraft.track is not None else '',
            'military':      bool(aircraft.db_flags & 1),
        })


_init_csv()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_bool(key: str, default: bool = True) -> bool:
    """
    Read a boolean from an environment variable.
    Accepts: true/false, yes/no, 1/0 (case-insensitive).
    Falls back to default if the key is not set.
    """
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ('true', 'yes', '1')


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KM_TO_NM = 0.539957  # kilometres → nautical miles

# Tracking website used in alert links — set TRACKING_URL in .env
# Options:
#   https://globe.adsbexchange.com/
#   https://globe.adsb.fi/
#   https://adsb.lol/
TRACKING_URL = os.getenv('TRACKING_URL', 'https://globe.adsb.fi/')

# ---------------------------------------------------------------------------
# Backoff settings
# ---------------------------------------------------------------------------
# On failure, an aggregator is skipped until its backoff period expires.
# Backoff doubles on each consecutive failure, capped at BACKOFF_MAX_SECONDS.
# 403 responses start at a higher initial backoff (rate limiting signal).
# After BACKOFF_ERROR_THRESHOLD consecutive failures, log level drops to WARNING
# to avoid flooding the log — a single recovery INFO is logged on success.
BACKOFF_BASE_SECONDS    = 30    # initial backoff (same as poll interval)
BACKOFF_403_SECONDS     = 120   # initial backoff for 403 specifically
BACKOFF_MAX_SECONDS     = 600   # cap at 10 minutes
BACKOFF_ERROR_THRESHOLD = 3     # failures before downgrading to WARNING

# ---------------------------------------------------------------------------
# Type code filter lists
# ---------------------------------------------------------------------------
# EXCLUDE: suppress alerts for these ICAO type codes.
#          Favourites always override EXCLUDE — a favourite is never suppressed.
# INCLUDE: always alert for these ICAO type codes regardless of military status.
# Set in .env as comma-separated values, e.g.:
#   EXCLUDE_TYPE_CODES=ULAC,P28A,C172
#   INCLUDE_TYPE_CODES=SPIT,HURI,P51,T6
# Ref: https://www.icao.int/publications/doc8643/pages/search.aspx

EXCLUDE_TYPE_CODES: set = {
    code.strip().upper()
    for code in os.getenv('EXCLUDE_TYPE_CODES', '').split(',')
    if code.strip()
}

INCLUDE_TYPE_CODES: set = {
    code.strip().upper()
    for code in os.getenv('INCLUDE_TYPE_CODES', '').split(',')
    if code.strip()
}


# ---------------------------------------------------------------------------
# Aggregator definitions
# ---------------------------------------------------------------------------
# Each aggregator has a corresponding AGGREGATOR_<NAME>_ENABLED env var.
# Set to false in .env to disable without touching the source code, e.g.:
#   AGGREGATOR_ADSBLOL_ENABLED=false
#   AGGREGATOR_ADSBFI_ENABLED=true
#   AGGREGATOR_AIRPLANESLIVE_ENABLED=false
#   AGGREGATOR_ADSBONE_ENABLED=false
#
# Local receivers are added separately via LOCAL_RECEIVER_URLS (see below).
#
# URL builder notes:
#   adsb.lol / adsb.fi / adsb.one – distance in nautical miles (radius_km * KM_TO_NM)
#   airplanes.live                – distance in kilometres      (radius_km directly)

def _adsbexchange_v2_parser(data: dict) -> List[dict]:
    """Standard parser for any ADSBexchange-v2-compatible JSON response."""
    return data.get('ac', []) or []


def _local_receiver_parser(data: dict) -> List[dict]:
    """
    Parser for local readsb/dump1090/tar1090 aircraft.json responses.
    These use 'aircraft' as the top-level key rather than 'ac'.
    Ref: http://<receiver>/data/aircraft.json
    """
    return data.get('aircraft', []) or []


AGGREGATORS: List[dict] = [
    {
        "name":    "adsb.fi",
        "env_key": "AGGREGATOR_ADSBFI_ENABLED",
        "enabled": _env_bool("AGGREGATOR_ADSBFI_ENABLED", default=True),
        # Ref: https://github.com/adsbfi/opendata
        "url_builder": lambda lat, lon, r: (
            f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{r * KM_TO_NM:.1f}"
        ),
        "parser":  _adsbexchange_v2_parser,
        "headers": {},
    },
    {
        "name":    "adsb.lol",
        "env_key": "AGGREGATOR_ADSBLOL_ENABLED",
        "enabled": _env_bool("AGGREGATOR_ADSBLOL_ENABLED", default=False),
        # Ref: https://api.adsb.lol/docs
        "url_builder": lambda lat, lon, r: (
            f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{r * KM_TO_NM:.1f}"
        ),
        "parser":  _adsbexchange_v2_parser,
        "headers": {},
    },
    {
        "name":    "adsb.one",
        "env_key": "AGGREGATOR_ADSBONE_ENABLED",
        "enabled": _env_bool("AGGREGATOR_ADSBONE_ENABLED", default=False),
        # Ref: https://api.adsb.one  — ADSBExchange v2 compatible, radius in nautical miles
        "url_builder": lambda lat, lon, r: (
            f"https://api.adsb.one/v2/point/{lat}/{lon}/{r * KM_TO_NM:.1f}"
        ),
        "parser":  _adsbexchange_v2_parser,
        "headers": {},
    },
    {
        "name":    "airplanes.live",
        "env_key": "AGGREGATOR_AIRPLANESLIVE_ENABLED",
        "enabled": _env_bool("AGGREGATOR_AIRPLANESLIVE_ENABLED", default=False),
        # Ref: https://airplanes.live/api-guide/
        # Uses kilometres, not nautical miles
        "url_builder": lambda lat, lon, r: (
            f"https://api.airplanes.live/v2/point/{lat}/{lon}/{r:.1f}"
        ),
        "parser":  _adsbexchange_v2_parser,
        "headers": {},
    },
]

# ---------------------------------------------------------------------------
# Local receiver injection
# ---------------------------------------------------------------------------
# LOCAL_RECEIVER_URLS accepts one or more local readsb/dump1090/tar1090 URLs,
# comma-separated. Each instance is added as a separate aggregator named
# local-1, local-2 etc. No radius filtering — all aircraft seen by the
# receiver are returned and filtered client-side by calculate_position().
#
# Example:
#   LOCAL_RECEIVER_URLS=http://zeewolf.local:1090/data/aircraft.json
#   LOCAL_RECEIVER_URLS=http://192.168.1.10:1090/data/aircraft.json,http://192.168.1.11:8080/data/aircraft.json
#
_local_urls = [
    url.strip()
    for url in os.getenv('LOCAL_RECEIVER_URLS', '').split(',')
    if url.strip()
]

for _i, _url in enumerate(_local_urls, start=1):
    _name = f"local-{_i}" if len(_local_urls) > 1 else "local"
    _fixed_url = _url  # capture for lambda closure
    AGGREGATORS.append({
        "name":        _name,
        "env_key":     None,  # not individually toggle-able; remove from env var to disable
        "enabled":     True,
        "url_builder": lambda lat, lon, r, u=_fixed_url: u,
        "parser":      _local_receiver_parser,
        "headers":     {},
    })
    logger.debug(f"Local receiver registered: {_name} → {_url}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    postcode: str
    radius_km: float = 15
    check_interval: int = 30
    favourites_file: str = None


# ---------------------------------------------------------------------------
# Aircraft dataclass
# ---------------------------------------------------------------------------
@dataclass
class Aircraft:
    icao24: str
    callsign: str
    type_code: str
    longitude: float
    latitude: float
    db_flags: int = 0
    alt_baro: Optional[float] = None
    gs: Optional[float] = None
    track: Optional[float] = None
    registration: str = ""

    @classmethod
    def from_adsbv2_data(cls, data: Dict) -> 'Aircraft':
        """
        ADSBexchange v2 compatible response (adsb.lol / adsb.fi / airplanes.live / adsb.one).
        Also compatible with local readsb/dump1090/tar1090 aircraft.json responses.
        Ref: https://api.adsb.lol/docs  /  https://github.com/airplanes-live/api-archive
        dbFlags bit 0 = military
        r = registration
        t = ICAO type code (prefix '19' = military category)
        track = true heading in degrees
        """
        alt = data.get('alt_baro')
        return cls(
            icao24=data.get('hex', ''),
            callsign=(data.get('flight') or '').strip(),
            type_code=data.get('t') or '',
            longitude=data.get('lon'),
            latitude=data.get('lat'),
            db_flags=data.get('dbFlags', 0),
            alt_baro=None if alt == 'ground' else alt,
            gs=data.get('gs'),
            track=data.get('track'),
            registration=data.get('r') or '',
        )

    def is_military(self) -> bool:
        """
        dbFlags bit 0 is the authoritative military flag set by the aggregator network database.
        type_code prefix '19' catches ICAO category A military aircraft not yet in the database.
        Ref: https://www.adsbexchange.com/version-2-api-wip/
        """
        return (
            bool(self.db_flags & 1) or
            (bool(self.type_code) and self.type_code.startswith('19'))
        )


# ---------------------------------------------------------------------------
# hexdb.io type lookup with in-memory cache
# ---------------------------------------------------------------------------
# Cache stores icao24 (lowercase) → full type string (or None if not found).
# Aircraft type never changes so no expiry is needed.
# Ref: https://hexdb.io/#api-body
_hexdb_cache: Dict[str, Optional[str]] = {}


def lookup_aircraft_type(icao24: str) -> Optional[str]:
    """
    Look up full aircraft type name from hexdb.io.
    Returns e.g. "C-130J Hercules" or None if not found.
    Results are cached in-memory for the lifetime of the process.
    """
    key = icao24.lower()
    if key in _hexdb_cache:
        return _hexdb_cache[key]
    try:
        response = requests.get(
            f"https://hexdb.io/api/v1/aircraft/{key}",
            timeout=5
        )
        if response.status_code == 200:
            data = response.json()
            # 'Type' is the full name e.g. "C-130J Hercules"
            # Fall back to 'ICAOTypeCode' if Type is absent
            result = data.get('Type') or data.get('ICAOTypeCode') or None
        else:
            result = None
    except Exception as e:
        logger.debug(f"hexdb.io lookup failed for {icao24}: {e}")
        result = None

    _hexdb_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class ApiClient:
    def __init__(self):
        # Apprise notification service
        # APPRISE_URLS accepts one or more Apprise-compatible URLs, comma-separated.
        # Ref: https://github.com/caronc/apprise/wiki
        self.apobj = apprise.Apprise()
        apprise_urls = os.getenv('APPRISE_URLS', '')
        if apprise_urls:
            for url in apprise_urls.split(','):
                url = url.strip()
                if url:
                    self.apobj.add(url)
        else:
            logger.warning("No APPRISE_URLS set — notifications disabled")

        # Per-aggregator backoff state: name → {failures, backoff_until}
        self._aggregator_state: Dict[str, Dict] = {
            a['name']: {'failures': 0, 'backoff_until': 0.0}
            for a in AGGREGATORS
        }

    def _record_aggregator_success(self, name: str):
        state = self._aggregator_state[name]
        if state['failures'] > 0:
            logger.info(f"✅ {name} recovered after {state['failures']} failure(s)")
        state['failures']      = 0
        state['backoff_until'] = 0.0

    def _record_aggregator_failure(self, name: str, status_code: Optional[int] = None):
        state = self._aggregator_state[name]
        state['failures'] += 1
        failures = state['failures']

        # 403 starts at a higher base (rate limiting); everything else uses standard base
        base    = BACKOFF_403_SECONDS if status_code == 403 else BACKOFF_BASE_SECONDS
        backoff = min(base * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS)
        state['backoff_until'] = time.time() + backoff

        # Only log at ERROR for first N failures; after that downgrade to WARNING
        # to avoid flooding the log during a prolonged outage
        log_fn = logger.error if failures <= BACKOFF_ERROR_THRESHOLD else logger.warning
        reason = f"HTTP {status_code}" if status_code else "connection error"
        log_fn(
            f"⚠️  {name} {reason} (failure #{failures}) — "
            f"backing off for {backoff:.0f}s"
        )

    def _aggregator_is_backed_off(self, name: str) -> bool:
        state = self._aggregator_state[name]
        if time.time() < state['backoff_until']:
            remaining = state['backoff_until'] - time.time()
            logger.debug(f"{name} in backoff — {remaining:.0f}s remaining, skipping")
            return True
        return False

    def get_postcode_location(self, postcode: str) -> Optional[Tuple[float, float]]:
        try:
            response = requests.get(
                f"https://api.postcodes.io/postcodes/{postcode}", timeout=10
            )
            data = response.json()
            if data['status'] == 200:
                result = data['result']
                return (result['longitude'], result['latitude'])
            return None
        except Exception as e:
            logger.error(f"Postcode API error: {e}")
            return None

    def get_aggregator_data(
        self,
        aggregator: dict,
        lat: float,
        lon: float,
        radius_km: float
    ) -> List[Aircraft]:
        """Generic fetcher for any ADSBexchange-v2-compatible aggregator."""
        name = aggregator['name']

        if self._aggregator_is_backed_off(name):
            return []

        url = aggregator['url_builder'](lat, lon, radius_km)
        try:
            response = requests.get(
                url,
                headers=aggregator.get('headers', {}),
                timeout=15
            )
            if response.status_code != 200:
                self._record_aggregator_failure(name, status_code=response.status_code)
                return []

            ac_list = aggregator['parser'](response.json())
            self._record_aggregator_success(name)
            return [
                Aircraft.from_adsbv2_data(ac)
                for ac in ac_list
                if ac.get('lat') is not None and ac.get('lon') is not None
            ]
        except Exception as e:
            self._record_aggregator_failure(name)
            logger.debug(f"{name} error detail: {e}")
            return []

    def get_aircraft_data(
        self,
        center: Tuple[float, float],
        radius_km: float
    ) -> List[Aircraft]:
        """
        Query all enabled aggregators concurrently.
        Aggregators in backoff are skipped for this poll.
        Deduplicate by icao24 — last writer wins among aggregators.
        """
        lon, lat = center
        results: Dict[str, Aircraft] = {}

        enabled = [a for a in AGGREGATORS if a['enabled']]

        with ThreadPoolExecutor(max_workers=len(enabled)) as executor:
            futures = {
                executor.submit(self.get_aggregator_data, a, lat, lon, radius_km): a['name']
                for a in enabled
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    aircraft_list = future.result()
                    for ac in aircraft_list:
                        results[ac.icao24.lower()] = ac
                    if aircraft_list:
                        logger.debug(f"{source}: {len(aircraft_list)} received")
                except Exception as e:
                    logger.error(f"Error processing {source} results: {e}")

        logger.debug(f"Combined aircraft count after deduplication: {len(results)}")
        return list(results.values())

    def send_alert(self, message: str) -> bool:
        try:
            return self.apobj.notify(title="Aircraft Alert", body=message)
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class AircraftMonitor:
    def __init__(self, config: Config):
        self.config            = config
        self.api               = ApiClient()
        self.active_aircraft   = set()
        self.favourites        = set()
        self._favourites_mtime = 0.0  # tracks last loaded mtime for hot-reload

    def load_favourites(self, filepath: str) -> set:
        try:
            with open(filepath, 'r') as f:
                return {
                    line.split('#')[0].strip().upper()
                    for line in f
                    if line.split('#')[0].strip()
                }
        except Exception as e:
            logger.error(f"Error loading favourites from {filepath}: {e}")
            return set()

    def _reload_favourites_if_changed(self):
        """Reload favourites file if it has been modified since last load."""
        if not self.config.favourites_file:
            return
        try:
            mtime = os.path.getmtime(self.config.favourites_file)
            if mtime != self._favourites_mtime:
                self.favourites        = self.load_favourites(self.config.favourites_file)
                self._favourites_mtime = mtime
                logger.info(f"⭐ Favourites reloaded — {len(self.favourites)} monitored")
        except OSError:
            # File temporarily unavailable (e.g. mid-save) — try again next poll
            pass

    def is_favourite(self, aircraft: Aircraft) -> bool:
        icao     = aircraft.icao24.upper()
        callsign = aircraft.callsign.strip().upper() if aircraft.callsign else ""
        return icao in self.favourites or callsign in self.favourites

    def calculate_position(self, aircraft: Aircraft, center: Tuple[float, float]) -> Dict:
        distance = self._haversine_distance(center, (aircraft.longitude, aircraft.latitude))
        bearing  = self._calculate_bearing(center, (aircraft.longitude, aircraft.latitude))
        return {
            'distance': distance,
            'bearing':  bearing,
            'cardinal': self._bearing_to_cardinal(bearing)
        }

    @staticmethod
    def _haversine_distance(
        coord1: Tuple[float, float],
        coord2: Tuple[float, float]
    ) -> float:
        lon1, lat1 = coord1
        lon2, lat2 = coord2
        R    = 6371
        dLat = radians(lat2 - lat1)
        dLon = radians(lon2 - lon1)
        a    = sin(dLat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dLon/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    @staticmethod
    def _calculate_bearing(
        center: Tuple[float, float],
        point:  Tuple[float, float]
    ) -> float:
        lon1, lat1 = radians(center[0]), radians(center[1])
        lon2, lat2 = radians(point[0]),  radians(point[1])
        d_lon  = lon2 - lon1
        y      = sin(d_lon) * cos(lat2)
        x      = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(d_lon)
        return (degrees(atan2(y, x)) + 360) % 360

    @staticmethod
    def _bearing_to_cardinal(bearing: float) -> str:
        directions = [
            (0,     'N'),   (22.5,  'NNE'), (45,    'NE'),  (67.5,  'ENE'),
            (90,    'E'),   (112.5, 'ESE'), (135,   'SE'),  (157.5, 'SSE'),
            (180,   'S'),   (202.5, 'SSW'), (225,   'SW'),  (247.5, 'WSW'),
            (270,   'W'),   (292.5, 'WNW'), (315,   'NW'),  (337.5, 'NNW')
        ]
        for limit, name in sorted(directions, reverse=True):
            if bearing >= limit:
                return name
        return 'N'

    def is_aircraft_active(self, icao24: str) -> bool:
        return icao24 in self.active_aircraft

    def mark_aircraft_active(self, icao24: str):
        self.active_aircraft.add(icao24)

    def remove_inactive_aircraft(self, current_icaos: set):
        self.active_aircraft = self.active_aircraft.intersection(current_icaos)

    def run(self):
        center_coords = self.api.get_postcode_location(self.config.postcode)
        if not center_coords:
            logger.error(f"Could not find coordinates for {self.config.postcode}")
            return

        # Initial favourites load
        self._reload_favourites_if_changed()

        enabled_aggregators = [a['name'] for a in AGGREGATORS if a['enabled']]
        logger.info(
            f"📡 Monitoring {self.config.radius_km}km radius around {self.config.postcode} "
            f"— aggregators: {', '.join(enabled_aggregators)}"
        )
        if EXCLUDE_TYPE_CODES:
            logger.info(f"🚫 Excluding type codes: {', '.join(sorted(EXCLUDE_TYPE_CODES))}")
        if INCLUDE_TYPE_CODES:
            logger.info(f"✅ Including type codes: {', '.join(sorted(INCLUDE_TYPE_CODES))}")

        while True:
            # Hot-reload favourites if the file has changed
            self._reload_favourites_if_changed()

            aircraft_data = self.api.get_aircraft_data(center_coords, self.config.radius_km)
            current_alert_icaos = set()
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")

            for aircraft in aircraft_data:
                # Favourites always win — never suppress a favourite even if type code is excluded
                if aircraft.type_code.upper() in EXCLUDE_TYPE_CODES and not self.is_favourite(aircraft):
                    continue

                position = self.calculate_position(aircraft, center_coords)
                if position['distance'] <= self.config.radius_km and (
                    aircraft.is_military() or
                    self.is_favourite(aircraft) or
                    aircraft.type_code.upper() in INCLUDE_TYPE_CODES
                ):
                    current_alert_icaos.add(aircraft.icao24)

                    if not self.is_aircraft_active(aircraft.icao24):
                        # Resolve full type name from hexdb.io, fall back to ICAO type code
                        aircraft_type = (
                            lookup_aircraft_type(aircraft.icao24)
                            or aircraft.type_code
                            or '?'
                        )
                        alt   = f"{int(aircraft.alt_baro)}ft" if aircraft.alt_baro is not None else '?'
                        gs    = f"{int(aircraft.gs)}kts"      if aircraft.gs is not None else '?'
                        track = f"{int(aircraft.track)}°"     if aircraft.track is not None else '?'
                        message = (
                            f"✈️ {aircraft_type} | {aircraft.registration or aircraft.callsign or '?'}\n"
                            f"🧭 {position['distance']:.1f}km {position['cardinal']} | {alt}\n"
                            f"🕧 {current_time}\n"
                            f"🔗 {TRACKING_URL}?icao={aircraft.icao24}"
                        )
                        logger.info("\n" + message + "\n")
                        self.api.send_alert(message)
                        log_alert_csv(aircraft)
                        self.mark_aircraft_active(aircraft.icao24)
                    else:
                        logger.debug(
                            f"Suppressed (already active): "
                            f"{aircraft.callsign} / {aircraft.icao24}"
                        )

            self.remove_inactive_aircraft(current_alert_icaos)
            time.sleep(self.config.check_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Monitor military aircraft in your area')
    default_postcode = os.getenv('POSTCODE')
    parser.add_argument('postcode', type=str, nargs='?', default=default_postcode,
                        help='Postcode to monitor (or set POSTCODE)')
    default_radius = os.getenv('RADIUS_KM', '15')
    parser.add_argument('-r', '--radius', type=float, default=default_radius,
                        help=f'Radius in kilometers to monitor (default: {default_radius})')
    default_favourites_file = os.getenv('FAVOURITES_FILE', 'data/favourites.txt')
    parser.add_argument('-f', '--favourites', type=str, default=default_favourites_file,
                        help='File path with favourite callsigns or ICAO identifiers '
                             f'(default: {default_favourites_file})')
    args = parser.parse_args()

    if not args.postcode:
        parser.error('a postcode is required either as an argument or via POSTCODE')

    monitor = AircraftMonitor(Config(
        postcode=args.postcode,
        radius_km=args.radius,
        favourites_file=args.favourites
    ))

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("🛑 Monitoring stopped by user (CTRL-C)")
