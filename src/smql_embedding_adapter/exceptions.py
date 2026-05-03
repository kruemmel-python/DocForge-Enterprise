"""Adapter-specific exceptions."""


class AdapterError(RuntimeError):
    """Base class for adapter errors."""


class ConfigurationError(AdapterError):
    """Invalid configuration."""


class EmbeddingError(AdapterError):
    """Embedding provider failed."""


class MyceliaGatewayError(AdapterError):
    """MyceliaDB Gateway failed."""


class StoreError(AdapterError):
    """Vector store failed."""


class SMQLError(AdapterError):
    """Invalid SMQL query."""
