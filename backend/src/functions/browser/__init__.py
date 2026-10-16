"""Browser escalation worker package for dynamic page rendering."""

from functions.browser.consumer import lambda_handler, process_batch, process_record
from functions.browser.driver import PlaywrightBrowserAdapter
from functions.browser.fixtures import FixtureBrowserAdapter
from functions.browser.models import SourceObservationModel
from functions.browser.network import (
    BrowserSecurityError,
    RouteSecurityInterceptor,
    SecureEgressProxy,
    is_ip_allowed,
    resolve_and_validate_host,
    validate_navigation_url,
)
from functions.browser.parser import parse_rendered_entities
from functions.browser.service import (
    BrowserDispatchEnvelope,
    BrowserWorkerService,
    parse_browser_dispatch_message,
)

__all__ = [
    "BrowserDispatchEnvelope",
    "BrowserSecurityError",
    "BrowserWorkerService",
    "FixtureBrowserAdapter",
    "PlaywrightBrowserAdapter",
    "RouteSecurityInterceptor",
    "SecureEgressProxy",
    "SourceObservationModel",
    "is_ip_allowed",
    "lambda_handler",
    "parse_browser_dispatch_message",
    "parse_rendered_entities",
    "process_batch",
    "process_record",
    "resolve_and_validate_host",
    "validate_navigation_url",
]
