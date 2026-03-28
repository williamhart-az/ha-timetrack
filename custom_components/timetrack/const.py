"""Constants for TimeTrack integration."""

DOMAIN = "timetrack"

# Config keys
CONF_PERSON_ENTITY = "person_entity"
CONF_MSP_URL = "msp_manager_url"
CONF_MSP_API_KEY = "msp_manager_api_key"
CONF_ROUNDING_MINUTES = "rounding_minutes"
CONF_MIN_SESSION_MINUTES = "min_session_minutes"
CONF_MSP_DRY_RUN = "msp_dry_run"
CONF_MSP_RESOURCE_ID = "msp_resource_id"

# Defaults
DEFAULT_PERSON_ENTITY = ""  # Must be set during config flow
DEFAULT_ROUNDING_MINUTES = 15
DEFAULT_MIN_SESSION_MINUTES = 15
DEFAULT_MSP_URL = "https://api.mspmanager.com/odata"

# Zone prefix for auto-discovery
TIMETRACK_ZONE_PREFIX = "TimeTrack - "

# Database
DB_FILE = "timetrack.db"

# Platforms
PLATFORMS = ["sensor", "binary_sensor"]
