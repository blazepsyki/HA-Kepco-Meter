"""Constants for the KEPCO ON integration."""

from homeassistant.const import CONF_PASSWORD, Platform

DOMAIN = "kepco_on"
NAME = "KEPCO ON"
VERSION = "0.4.0"
CONFIG_ENTRY_VERSION = 3
# Scope the user-reported request and Wh conversion to the exact tested contract.
COMBINED_APARTMENT_PLANNER_CONTRACT = "아파트(종합계약)"

BASE_URL = "https://online.kepco.co.kr"
PAGE_URL = "https://online.kepco.co.kr/MYM001D00"

ENDPOINT_LOGIN_INDI = "/cyb/me/login/indi/api"
ENDPOINT_SESSION_CHECK = "/sessionCheck"
ENDPOINT_IS_CORP = "/isCorp"
ENDPOINT_MYPAGE_CUST_NO_LIST = "/my/indi/info/myPageCustNoList"
ENDPOINT_APT_BILL_DETAIL = "/my/charge/pay/aptBillDetail"
ENDPOINT_MAIN_CHART = "/my/memo/mainChart"
ENDPOINT_HOUSE_BILL_DETAIL = "/my/charge/view/"
ENDPOINT_POWER_PLANNER = "/my/memo/powerPlanner"

POLLING_INTERVAL_HOURS = (1, 3, 6, 12, 24)
DEFAULT_POLLING_INTERVAL_HOURS = 6
DEFAULT_CO2_FACTOR_KG_PER_KWH = 0.459

PLATFORMS = (Platform.SENSOR,)

CONF_USERNAME = "username"
CONF_SAVE_PASSWORD = "save_password"
CONF_SELECTED_CUSTOMERS = "selected_customers"
CONF_ACCOUNT_UID_HASH = "account_uid_hash"
CONF_CUSTOMERS = "customers"
CONF_DISPLAY_NAME = "display_name"
CONF_SESSION_HANDOFF = "session_handoff"
# Setup consumes the handoff into the private Store and removes it from entry data.
SENSITIVE_CONFIG_DATA_KEYS = frozenset({CONF_PASSWORD, CONF_SESSION_HANDOFF})

DATA_STABLE_KEY = "stable_key"
DATA_APARTMENT_NAME = "apartment_name"
DATA_DONG = "dong"
DATA_HO = "ho"
DATA_CONTRACT_METHOD = "contract_method"
DATA_IS_SUPPORTED = "is_supported"
DATA_CUSTOMER_NUMBER = "customer_number"
DATA_HOUSE_CONTRACT_NUMBER = "house_contract_number"

OPT_POLLING_INTERVAL_HOURS = "polling_interval_hours"
OPT_ENABLE_DETAILED_SENSORS = "enable_detailed_sensors"
OPT_ENABLE_CO2_ESTIMATE = "enable_co2_estimate"
OPT_CO2_FACTOR_KG_PER_KWH = "co2_factor_kg_per_kwh"
OPT_HISTORY_MONTHS = "history_months"
OPT_ENABLE_POWER_PLANNER = "enable_power_planner"
OPT_POWER_PLANNER_CUSTOMER_ID = "power_planner_customer_id"

# Only the two KEPCO ON authentication-session cookies are persisted. The
# session store still restricts them to KEPCO domains, valid paths and expiry.
CANDIDATE_COOKIE_NAMES: frozenset[str] = frozenset({"JSESSIONID", "kepcoSSO"})
PERSISTED_COOKIE_ALLOWLIST: frozenset[str] = CANDIDATE_COOKIE_NAMES

POWER_PLANNER_SOURCE_KEYS: dict[str, frozenset[str]] = {
    "tier": frozenset({"power_planner_current_bill_level"}),
    "month": frozenset({"power_planner_calendar_month_usage"}),
    "year": frozenset({"power_planner_calendar_year_usage"}),
    "day_comparison": frozenset(
        {
            "power_planner_prior_year_same_day_usage",
            "power_planner_same_day_year_on_year_usage_difference",
            "power_planner_same_day_year_on_year_usage_ratio",
        }
    ),
    "neighbors": frozenset(
        {
            "power_planner_regional_average_usage",
            "power_planner_saving_neighbor_usage",
            "power_planner_regional_usage_percentile",
            "power_planner_regional_average_amount",
            "power_planner_saving_neighbor_amount",
            "power_planner_regional_amount_percentile",
        }
    ),
}
POWER_PLANNER_DEFAULT_SOURCES = frozenset({"tier", "month", "year"})

__all__ = [
    "BASE_URL",
    "CANDIDATE_COOKIE_NAMES",
    "COMBINED_APARTMENT_PLANNER_CONTRACT",
    "CONFIG_ENTRY_VERSION",
    "CONF_ACCOUNT_UID_HASH",
    "CONF_CUSTOMERS",
    "CONF_DISPLAY_NAME",
    "CONF_PASSWORD",
    "CONF_SAVE_PASSWORD",
    "CONF_SELECTED_CUSTOMERS",
    "CONF_SESSION_HANDOFF",
    "CONF_USERNAME",
    "DATA_APARTMENT_NAME",
    "DATA_CONTRACT_METHOD",
    "DATA_CUSTOMER_NUMBER",
    "DATA_DONG",
    "DATA_HO",
    "DATA_HOUSE_CONTRACT_NUMBER",
    "DATA_IS_SUPPORTED",
    "DATA_STABLE_KEY",
    "DEFAULT_CO2_FACTOR_KG_PER_KWH",
    "DEFAULT_POLLING_INTERVAL_HOURS",
    "DOMAIN",
    "ENDPOINT_APT_BILL_DETAIL",
    "ENDPOINT_HOUSE_BILL_DETAIL",
    "ENDPOINT_IS_CORP",
    "ENDPOINT_LOGIN_INDI",
    "ENDPOINT_MAIN_CHART",
    "ENDPOINT_MYPAGE_CUST_NO_LIST",
    "ENDPOINT_POWER_PLANNER",
    "ENDPOINT_SESSION_CHECK",
    "NAME",
    "OPT_CO2_FACTOR_KG_PER_KWH",
    "OPT_ENABLE_CO2_ESTIMATE",
    "OPT_ENABLE_DETAILED_SENSORS",
    "OPT_ENABLE_POWER_PLANNER",
    "OPT_HISTORY_MONTHS",
    "OPT_POLLING_INTERVAL_HOURS",
    "OPT_POWER_PLANNER_CUSTOMER_ID",
    "PAGE_URL",
    "PERSISTED_COOKIE_ALLOWLIST",
    "PLATFORMS",
    "POLLING_INTERVAL_HOURS",
    "POWER_PLANNER_DEFAULT_SOURCES",
    "POWER_PLANNER_SOURCE_KEYS",
    "SENSITIVE_CONFIG_DATA_KEYS",
    "VERSION",
]
