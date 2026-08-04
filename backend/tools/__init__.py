"""Deterministic tools the tutor model may call.

Pure computation. Nothing here knows about prompts, models, the database, or HTTP, and
nothing here reads or writes outside its own process. That is what makes these testable
against known results, which is the point: this is the one place in Lyra where the
expected value of a computation can be asserted exactly.
"""
