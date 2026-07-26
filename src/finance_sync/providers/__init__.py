"""Data providers for external market data sources.

Each provider is a standalone class that wraps a specific data source
(API or SDK), handling authentication, error mapping, and rate limiting.
Providers return simple Python types and are designed to be composed
into higher-level services.
"""
