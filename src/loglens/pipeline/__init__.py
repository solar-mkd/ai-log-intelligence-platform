"""Medallion pipeline steps: bronze -> silver -> gold.

Each step is a stateless unit that reads its work from storage/config and can
run independently (ADR-014). See docs/architecture.md for the data flow.
"""
