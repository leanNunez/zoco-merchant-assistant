"""Part 4: multi-turn conversations, repeated, judged by assertions on observable facts.

`python -m evaluation.conversations run --mode scripted|null|naive|live` runs every scenario of
`catalog.py` in a fresh world built by the real composition root and writes the results under
`evaluation/results/conversations/<mode>/`. ADR-0006 explains the design.
"""
