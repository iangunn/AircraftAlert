# Standard library imports
import argparse
from dataclasses import dataclass
import logging
from math import atan2, cos, degrees, radians, sin, sqrt
import os
import time
from typing import Dict, List, Optional, Tuple

# Third-party imports
import apprise
from dotenv import load_dotenv
import requests

# Load environment variables
load_dotenv()

# Update the log file path to use a 'logs' directory
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

@dataclass
class Config:
    postcode: str
    radius_km: float = 15
    check_interval: int = 120
    uk_bounds: Dict[str, float] = None
    favourites_file: str = None  # new config property for favourites file
    
    def __post_init__(self):
        self.uk_bounds = {
            'lamin': 49.5, 'lamax': 61.0,
            'lomin': -9.0, 'lomax': 2.0
        }

class ApiClient:
    def __init__(self):
        self.opensky_auth = (
            os.getenv('OPENSKY_USERNAME'),
            os.getenv('OPENSKY_PASSWORD')
        )
        # Initialize apprise
        self.apobj = apprise.Apprise()
        # Add Pushover using environment variables
        pushover_url = f"pover://{os.getenv('PUSHOVER_USER')}@{os.getenv('PUSHOVER_TOKEN')}"
        self.apobj.add(pushover_url)

    def get_postcode_location(self, postcode: str) -> Optional[Tuple[float, float]]:
        try:
            response = requests.get(f"https://api.postcodes.io/postcodes/{postcode}")
            data = response.json()
            if data['status'] == 200:
                result = data['result']
                return (result['longitude'], result['latitude'])
            return None
        except Exception as e:
            logger.error(f"Postcode API error: {e}")
            return None

    def get_aircraft_data(self, bounds: Dict[str, float]) -> List:
        try:
            response = requests.get(
                "https://opensky-network.org/api/states/all",
                params=bounds,
                auth=self.opensky_auth
            )
            if response.status_code != 200:
                logger.error(f"OpenSky API error: {response.status_code}")
                return []
            return response.json().get('states', []) or []  # Ensure it returns a list
        except Exception as e:
            logger.error(f"Error fetching aircraft data: {e}")
            return []  # Return an empty list on error

    def send_alert(self, message: str) -> bool:
        try:
            return self.apobj.notify(
                title="Aircraft Alert",
                body=message
            )
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False

@dataclass
class Aircraft:
    icao24: str
    callsign: str
    type_code: str
    longitude: float
    latitude: float

    @classmethod
    def from_api_data(cls, data: List) -> 'Aircraft':
        return cls(
            icao24=data[0],
            callsign=data[1].strip() if data[1] else "",
            type_code=data[2] if data[2] else "",
            longitude=data[5],
            latitude=data[6]
        )

    def is_military(self) -> bool:
        military_prefixes = ('43', 'AE') # ('0D', '0E', '0F', '30', '34', '39', '3F', '43', '480', '4B', 'AE', 'ADF', 'C0')
        military_callsigns = ('MIL', 'NOW', "ARR", "RRR", "RAF", "NATO", "AAC", "NAF", "PLF", "TTN", "XXXX", "00000000")
        
        return (
            self.icao24.upper().startswith(military_prefixes) or
            self.callsign.startswith(military_callsigns) or
            (self.type_code and self.type_code.startswith('19'))
        )

class AircraftMonitor:
    def __init__(self, config: Config):
        self.config = config
        self.api = ApiClient()
        self.active_aircraft = set()  # Track aircraft we've already alerted about
        self.favourites = set()       # new attribute for favourites
        
    def load_favourites(self, filepath: str) -> set:
        try:
            with open(filepath, 'r') as f:
                return {line.strip().upper() for line in f if line.strip()}
        except Exception as e:
            logger.error(f"Error loading favourites from {filepath}: {e}")
            return set()

    def is_favourite(self, aircraft: Aircraft) -> bool:
        # Check if aircraft ICAO or callsign is in favourites
        return (aircraft.icao24.upper() in self.favourites or
                (aircraft.callsign.strip().upper() if aircraft.callsign else "") in self.favourites)
        
    def calculate_position(self, aircraft: Aircraft, center: Tuple[float, float]) -> Dict:
        distance = self._haversine_distance(center, (aircraft.longitude, aircraft.latitude))
        bearing = self._calculate_bearing(center, (aircraft.longitude, aircraft.latitude))
        return {
            'distance': distance,
            'bearing': bearing,
            'cardinal': self._bearing_to_cardinal(bearing)
        }

    @staticmethod
    def _haversine_distance(coord1: Tuple[float, float], coord2: Tuple[float, float]) -> float:
        lon1, lat1 = coord1
        lon2, lat2 = coord2
        
        R = 6371  # Earth radius in km
        dLat = radians(lat2 - lat1)
        dLon = radians(lon2 - lon1)
        a = sin(dLat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dLon/2)**2
        c = 2 * atan2(sqrt(a), sqrt(1-a))
        return R * c

    @staticmethod
    def _calculate_bearing(center: Tuple[float, float], point: Tuple[float, float]) -> float:
        lon1, lat1 = radians(center[0]), radians(center[1])
        lon2, lat2 = radians(point[0]), radians(point[1])

        d_lon = lon2 - lon1
        y = sin(d_lon) * cos(lat2)
        x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(d_lon)
        bearing = degrees(atan2(y, x))
        return (bearing + 360) % 360

    @staticmethod
    def _bearing_to_cardinal(bearing: float) -> str:
        directions = [
            (0, 'N'), (22.5, 'NNE'), (45, 'NE'), (67.5, 'ENE'),
            (90, 'E'), (112.5, 'ESE'), (135, 'SE'), (157.5, 'SSE'),
            (180, 'S'), (202.5, 'SSW'), (225, 'SW'), (247.5, 'WSW'),
            (270, 'W'), (292.5, 'WNW'), (315, 'NW'), (337.5, 'NNW')
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
        # Remove aircraft that are no longer in the area
        self.active_aircraft = self.active_aircraft.intersection(current_icaos)

    def run(self):
        center_coords = self.api.get_postcode_location(self.config.postcode)
        if not center_coords:
            logger.error(f"Could not find coordinates for {self.config.postcode}")
            return

        # Load favourites from file if provided
        if self.config.favourites_file:
            self.favourites = self.load_favourites(self.config.favourites_file)
            logger.info(f"Favourites loaded: {self.favourites}")

        logger.info(f"📡 Monitoring {self.config.radius_km}km radius around {self.config.postcode}")
        
        while True:
            aircraft_data = self.api.get_aircraft_data(self.config.uk_bounds)
            current_alert_icaos = set()  # track aircraft we've alerted for (military or favourites)
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            
            for data in aircraft_data:
                aircraft = Aircraft.from_api_data(data)
                position = self.calculate_position(aircraft, center_coords)
                if position['distance'] <= self.config.radius_km and (aircraft.is_military() or self.is_favourite(aircraft)):
                    current_alert_icaos.add(aircraft.icao24)
                    
                    if not self.is_aircraft_active(aircraft.icao24):
                        message = ( 
                            f"🕧 {current_time}\n"
                            f"🧭 {position['distance']:.1f}km {position['cardinal']} ({position['bearing']:.0f}°)\n"
                            f"✈️ {aircraft.callsign} / {aircraft.icao24}\n"
                            f"🔗 https://globe.adsbexchange.com/?icao={aircraft.icao24}"
                        )
                        logger.info("\n" + message + "\n")
                        self.api.send_alert(message)
                        self.mark_aircraft_active(aircraft.icao24)
            
            self.remove_inactive_aircraft(current_alert_icaos)
            time.sleep(self.config.check_interval)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Monitor military aircraft in your area')
    parser.add_argument('postcode', type=str, help='Postcode to monitor (required)')
    parser.add_argument('-r', '--radius', type=float, default=15,
                      help='Radius in kilometers to monitor (default: 15)')
    parser.add_argument('-f', '--favourites', type=str,
                      help='File path with favourite callsigns or ICAO identifiers (optional)')
    
    args = parser.parse_args()
    
    monitor = AircraftMonitor(Config(
        postcode=args.postcode, 
        radius_km=args.radius,
        favourites_file=args.favourites  # pass favourites file to config
    ))
    monitor.run()