"""providers — engine I/O atoms: one brain-shape per backend.

Each provider implements ProviderBase (base.py) so the loop is backend-agnostic.
Layer: echelon_engine.atoms (leaf I/O). May import echelon_sdk only — never boards/services/apps.
"""
