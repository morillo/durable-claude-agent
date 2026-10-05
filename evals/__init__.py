"""Evaluation harness: runs the dataset through the real workflow and scores five things.

(a) execution accuracy   gold SQL vs generated SQL result sets on DuckDB
(b) risk classification  deterministic class vs expected class
(c) retrieval recall@k   expected governance sections among retrieved passages
(d) tool-use judge       Claude grades whether tool calls were necessary and well-sequenced
(e) approval behavior    policy-violating runs reached needs_approval/blocked and never executed first

Entry point: ``python -m evals.runner`` (``make eval``).
"""
