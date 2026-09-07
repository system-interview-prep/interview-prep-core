"""Similarity algorithms package"""

from .cosine_similarity import CosineSimilarityAnalyzer
from .jaccard_similarity import JaccardSimilarityAnalyzer
from .bm25_analyzer import BM25Analyzer
from .requirements_analyzer import RequirementsAnalyzer
from .must_have_analyzer import MustHaveAnalyzer
try:
    from .ner_analyzer import NERAnalyzer
except ImportError:
    NERAnalyzer = None

__all__ = [
    'CosineSimilarityAnalyzer',
    'JaccardSimilarityAnalyzer',
    'BM25Analyzer',
    'RequirementsAnalyzer',
    'MustHaveAnalyzer',
    'NERAnalyzer'
]

