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
log_directory = 'logs'
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
CSV_FIELDS = ['date', 'time', 'icao24', 'registration', 'callsign', 'type_code',
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
            'date':         now.strftime('%Y-%m-%d'),
            'time':         now.strftime('%H:%M:%S'),
            'icao24':       aircraft.icao24,
            'registration': aircraft.registration,
            'callsign':     aircraft.callsign,
            'type_code':    aircraft.type_code,
            'lat':          aircraft.latitude,
            'lon':          aircraft.longitude,
            'alt_baro':     aircraft.alt_baro if aircraft.alt_baro is not None else '',
            'gs':           aircraft.gs if aircraft.gs is not None else '',
            'track':        aircraft.track if aircraft.track is not None else '',
            'military':     bool(aircraft.db_flags & 1),
        })

_init_csv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KM_TO_NM = 0.539957  # kilometres → nautical miles

# Tracking website used in alert links — change to preferred viewer
# Options:
#   https://globe.adsbexchange.com/
#   https://globe.adsb.fi/
#   https://adsb.lol/
TRACKING_URL = "https://adsb.lol/"

# ---------------------------------------------------------------------------
# Type code exclusion and inclusion lists
# ---------------------------------------------------------------------------
# Comma-separated ICAO type codes
# Set in .env as: EXCLUDE_TYPE_CODES=ULAC,P28A,C172
# Ref: https://www.icao.int/publications/doc8643/pages/search.aspx
_raw_exclusions = os.getenv('EXCLUDE_TYPE_CODES', '')
EXCLUDE_TYPE_CODES: set = {
    code.strip().upper()
    for code in _raw_exclusions.split(',')
    if code.strip()
}

_raw_inclusions = os.getenv('INCLUDE_TYPE_CODES', '')
INCLUDE_TYPE_CODES: set = {
    code.strip().upper()
    for code in _raw_inclusions.split(',')
    if code.strip()
}

# ---------------------------------------------------------------------------
# Feeder definitions
# ---------------------------------------------------------------------------
# Each entry describes one ADS-B data source.
# Fields:
#   name        – human-readable label used in log messages
#   enabled     – set False to skip this source entirely
#   url_builder – callable(lat, lon, radius_km) → str  (builds the request URL)
#   parser      – callable(response_json) → List[dict] (extracts the ac list)
#   headers     – optional dict of extra HTTP headers (e.g. API keys)
#
# URL builder notes:
#   adsb.lol  / adsb.fi   – distance in nautical miles  (radius_km * KM_TO_NM)
#   airplanes.live        – distance in kilometres       (radius_km directly)

def _adsbexchange_v2_parser(data: dict) -> List[dict]:
    """Standard parser for any ADSBexchange-v2-compatible JSON response."""
    return data.get('ac', []) or []

FEEDERS: List[dict] = [
    {
        "name": "adsb.lol",
        "enabled": True,
        "url_builder": lambda lat, lon, r: (
            f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{r * KM_TO_NM:.1f}"
        ),
        "parser": _adsbexchange_v2_parser,
        "headers": {},
    },
    {
        "name": "adsb.fi",
        "enabled": True,
        "url_builder": lambda lat, lon, r: (
            f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{r * KM_TO_NM:.1f}"
        ),
        "parser": _adsbexchange_v2_parser,
        "headers": {},
    },
    {
        "name": "airplanes.live",
        "enabled": True,
        # airplanes.live uses /point/<lat>/<lon>/<radius_km> — distance in km
        # Ref: https://airplanes.live/api-guide/
        "url_builder": lambda lat, lon, r: (
            f"https://api.airplanes.live/v2/point/{lat}/{lon}/{r:.1f}"
        ),
        "parser": _adsbexchange_v2_parser,
        "headers": {},
    },
]


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
        ADSBexchange v2 compatible response (adsb.lol / adsb.fi / airplanes.live).
        Ref: https://api.adsb.lol/docs  /  https://github.com/airplanes-live/api
        dbFlags bit 0 = military
        r = registration
        t = ICAO type code (prefix '19' = military category)
        track = true heading in degrees
        """
        return cls(
            icao24=data.get('hex', ''),
            callsign=(data.get('flight') or '').strip(),
            type_code=data.get('t') or '',
            longitude=data.get('lon'),
            latitude=data.get('lat'),
            db_flags=data.get('dbFlags', 0),
            alt_baro=data.get('alt_baro'),
            gs=data.get('gs'),
            track=data.get('track'),
            registration=data.get('r') or '',
        )

    def is_military(self) -> bool:
        """
        dbFlags bit 0 is the authoritative military flag set by the feeder network database.
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
        self.apobj = apprise.Apprise()
        pushover_url = f"pover://{os.getenv('PUSHOVER_USER')}@{os.getenv('PUSHOVER_TOKEN')}"
        self.apobj.add(pushover_url)

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

    def get_feeder_data(
        self,
        feeder: dict,
        lat: float,
        lon: float,
        radius_km: float
    ) -> List[Aircraft]:
        """Generic fetcher for any ADSBexchange-v2-compatible feeder."""
        url = feeder['url_builder'](lat, lon, radius_km)
        try:
            response = requests.get(
                url,
                headers=feeder.get('headers', {}),
                timeout=15
            )
            if response.status_code != 200:
                logger.error(f"{feeder['name']} API error: {response.status_code}")
                return []
            ac_list = feeder['parser'](response.json())
            return [
                Aircraft.from_adsbv2_data(ac)
                for ac in ac_list
                if ac.get('lat') is not None and ac.get('lon') is not None
            ]
        except Exception as e:
            logger.error(f"{feeder['name']} fetch error: {e}")
            return []

    def get_aircraft_data(
        self,
        center: Tuple[float, float],
        radius_km: float
    ) -> List[Aircraft]:
        """
        Query all enabled feeders concurrently.
        Deduplicate by icao24 — last writer wins among feeders.
        """
        lon, lat = center
        results: Dict[str, Aircraft] = {}

        enabled = [f for f in FEEDERS if f['enabled']]

        with ThreadPoolExecutor(max_workers=len(enabled)) as executor:
            futures = {
                executor.submit(self.get_feeder_data, f, lat, lon, radius_km): f['name']
                for f in enabled
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    aircraft_list = future.result()
                    for ac in aircraft_list:
                        results[ac.icao24.lower()] = ac
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
        self.config = config
        self.api    = ApiClient()
        self.active_aircraft = set()
        self.favourites      = set()

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

        if self.config.favourites_file:
            self.favourites = self.load_favourites(self.config.favourites_file)

        enabled_feeders = [f['name'] for f in FEEDERS if f['enabled']]
        logger.info(f"📡 Monitoring {self.config.radius_km}km radius around {self.config.postcode}")
        logger.info(f"ℹ️ Sources: {', '.join(enabled_feeders)}")

        if INCLUDE_TYPE_CODES:
            logger.info(f"✅ Including type codes: {', '.join(sorted(INCLUDE_TYPE_CODES))}")
        if EXCLUDE_TYPE_CODES:
            logger.info(f"🚫 Excluding type codes: {', '.join(sorted(EXCLUDE_TYPE_CODES))}")

        logger.info(f"⭐ Included {len(self.favourites)} favourites")

        while True:
            aircraft_data = self.api.get_aircraft_data(center_coords, self.config.radius_km)
            current_alert_icaos = set()
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")

            for aircraft in aircraft_data:
                # Skip excluded type codes
                if aircraft.type_code.upper() in EXCLUDE_TYPE_CODES:
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
                            f"🧭 {position['cardinal']} | {alt}\n"
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
    parser.add_argument('postcode', type=str, help='Postcode to monitor (required)')
    parser.add_argument('-r', '--radius', type=float, default=15,
                        help='Radius in kilometers to monitor (default: 15)')
    parser.add_argument('-f', '--favourites', type=str, default='./favourites.txt',
                        help='File path with favourite callsigns or ICAO identifiers '
                             '(default: ./favourites.txt)')
    args = parser.parse_args()

    monitor = AircraftMonitor(Config(
        postcode=args.postcode,
        radius_km=args.radius,
        favourites_file=args.favourites
    ))

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("🛑 Monitoring stopped by user (CTRL-C)")
