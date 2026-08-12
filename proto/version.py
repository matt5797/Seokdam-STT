"""Application version embedded in release builds."""

try:
    from build_version import APP_VERSION
except ImportError:
    APP_VERSION = "1.1.0-dev"
