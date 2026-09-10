"""echelon_engine.agent -- the orchestration spine (agent layer).

Layer: apps -> echelon_engine.services -> echelon_engine.boards
            -> echelon_engine.agent   (HERE) -> echelon_engine.atoms -> echelon_sdk

Modules (port order):
  rolebook    -- per-role disk-books, role/tier cartridge routing
  fork_field  -- warmth-clocked fork-field scheduler
  workflow    -- wave-based DAG scheduler + agent runner
  board       -- shared ledger + run_board
  partner     -- dispatch / swarm / board entrypoints (the culmination)
"""
