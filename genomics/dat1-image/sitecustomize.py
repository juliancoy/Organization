"""NetworkX 1.11 imports the gcd alias removed from fractions in Python 3.9.

Restore that standard-library alias only; do not modify HMM/caller algorithms.
"""
import fractions
import math

if not hasattr(fractions, 'gcd'):
    fractions.gcd = math.gcd
