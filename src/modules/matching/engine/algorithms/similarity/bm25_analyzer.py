import re
from ..base_algorithm import BaseAlgorithm
import logging
from collections import Counter
import math

logger = logging.getLogger(__name__)

class BM25Analyzer(BaseAlgorithm):
    """
    BM25 (Best Matching 25) Ranking Algorithm
    
    Used for term frequency-based relevance calculation with term saturation and document length normalization.
    """
    
    def __init__(self, config: dict = None):
        super().__init__('bm25', config)
        self.k1 = float(self.config.get('k1', 1.5))  # Term saturation parameter (1.2-2.0)
        self.b = float(self.config.get('b', 0.75))   # Document length normalization (0-1)
        self.tech_skills = self._load_tech_skills()
    
    def _load_tech_skills(self) -> set:
        """Load technical skills for boosting"""
        return {
            'python', 'java', 'javascript', 'typescript', 'react', 'angular', 'vue',
            'nodejs', 'django', 'flask', 'spring', 'aws', 'azure', 'docker',
            'kubernetes', 'mongodb', 'postgresql', 'mysql', 'redis', 'git', 'c++',
            'c#', 'go', 'rust', 'php', 'ruby', 'swift', 'kotlin', 'scala', 'c',
            'html', 'css', 'tailwind', 'bootstrap', 'scikit-learn', 'tensorflow',
            'pytorch', 'fastapi', 'nextjs', 'nestjs', 'gcp', 'jenkins', 'terraform'
        }
    
    def load_model(self):
        self.is_loaded = True
        logger.info("BM25 ranking algorithm initialized")
    
    def _tokenize(self, text: str) -> list:
        """Tokenize text into terms"""
        text = text.lower()
        # Keep alphanumeric and common tech separators
        text = re.sub(r'[^\w\s\-\+\.]', ' ', text)
        tokens = text.split()
        # Remove very short tokens unless they are skills (like 'c', 'r', 'go')
        return [t for t in tokens if len(t) > 2 or t in {'c', 'r', 'go'}]
    
    def _compute_idf(self, term: str, doc_count: int, term_doc_freq: int) -> float:
        """Compute IDF (Inverse Document Frequency) for BM25"""
        idf = math.log((doc_count - term_doc_freq + 0.5) / (term_doc_freq + 0.5) + 1.0)
        return max(0.0, idf)  # Ensure non-negative
    
    def _compute_bm25_score(self, term_freq: int, doc_length: int, 
                           avg_doc_length: float, idf: float) -> float:
        """
        Compute BM25 score for a term
        score = IDF * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (doc_len / avg_doc_len)))
        """
        numerator = term_freq * (self.k1 + 1)
        denominator = term_freq + self.k1 * (1 - self.b + self.b * (doc_length / avg_doc_length))
        return idf * (numerator / denominator)
    
    def _extract_n_grams(self, tokens: list, n: int = 2) -> list:
        """Extract n-grams for phrase matching"""
        ngrams = []
        for i in range(len(tokens) - n + 1):
            ngram = ' '.join(tokens[i:i+n])
            ngrams.append(ngram)
        return ngrams
    
    def process_single(self, resume_text: str, job_description: str, 
                       position: str = None) -> dict:
        """Rank resume using BM25 algorithm"""
        if not self.is_loaded:
            self.load_model()
        
        try:
            resume_tokens = self._tokenize(resume_text)
            job_tokens = self._tokenize(job_description)
            
            resume_bigrams = self._extract_n_grams(resume_tokens, 2)
            job_bigrams = self._extract_n_grams(job_tokens, 2)
            
            resume_terms = resume_tokens + resume_bigrams
            job_terms = job_tokens + job_bigrams
            
            resume_length = len(resume_terms)
            job_length = len(job_terms)
            avg_length = (resume_length + job_length) / 2
            
            resume_term_freq = Counter(resume_terms)
            
            query_terms = set(job_terms)
            
            total_bm25_score = 0.0
            term_scores = {}
            matched_terms = []
            
            # Treating resume and job as a corpus of 2 docs
            doc_count = 2
            
            for term in query_terms:
                if term in resume_term_freq:
                    term_doc_freq = 2  # Appears in both
                    idf = self._compute_idf(term, doc_count, term_doc_freq)
                    
                    tf = resume_term_freq[term]
                    bm25_term_score = self._compute_bm25_score(
                        tf, resume_length, avg_length, idf
                    )
                    
                    if term in self.tech_skills:
                        bm25_term_score *= 1.5  # 50% boost for tech skills
                    
                    total_bm25_score += bm25_term_score
                    term_scores[term] = bm25_term_score
                    matched_terms.append(term)
            
            # Normalize BM25 score
            max_possible_score = len(query_terms) * 10
            normalized_score = total_bm25_score / max_possible_score if max_possible_score > 0 else 0
            
            coverage = len(matched_terms) / len(query_terms) if query_terms else 0
            
            rare_term_bonus = 0.0
            for term in matched_terms:
                if len(term) > 8:
                    rare_term_bonus += 0.01
            
            exact_phrases = set(job_bigrams) & set(resume_bigrams)
            phrase_bonus = min(0.15, len(exact_phrases) * 0.02)
            
            base_score = normalized_score * 0.60 + coverage * 0.40
            final_score = base_score + rare_term_bonus + phrase_bonus
            
            # Clip between [0.0, 1.0] for the new architecture (avoid artificial max cap)
            final_score = max(0.0, min(1.0, final_score))
            
            top_terms = sorted(term_scores.items(), key=lambda x: x[1], reverse=True)[:10]
            matching_skills = sorted(set(matched_terms) & self.tech_skills)
            
            logger.info(f"BM25 - Raw:{total_bm25_score:.2f}, Normalized:{normalized_score:.2f}, Final:{final_score:.2f}")
            
            return {
                'algorithm': self.name,
                'score': float(final_score),
                'details': {
                    'algorithm_type': 'BM25 (Best Matching 25)',
                    'raw_bm25_score': total_bm25_score,
                    'normalized_bm25': normalized_score,
                    'coverage_ratio': coverage,
                    'parameters': {
                        'k1': self.k1,
                        'b': self.b
                    },
                    'statistics': {
                        'resume_length': resume_length,
                        'job_length': job_length,
                        'avg_length': avg_length,
                        'unique_query_terms': len(query_terms),
                        'matched_terms': len(matched_terms),
                        'exact_phrase_matches': len(exact_phrases)
                    },
                    'top_matching_terms': [
                        {'term': term, 'bm25_score': float(score)} 
                        for term, score in top_terms
                    ],
                    'matching_skills': matching_skills,
                    'bonuses': {
                        'rare_term_bonus': rare_term_bonus,
                        'phrase_bonus': phrase_bonus
                    }
                }
            }
            
        except Exception as e:
            logger.error(f"BM25 ranking failed: {e}", exc_info=True)
            return {'algorithm': self.name, 'score': 0.0, 'details': {'error': str(e)}}

