import re
from ..base_algorithm import BaseAlgorithm
import logging

logger = logging.getLogger(__name__)

class JaccardSimilarityAnalyzer(BaseAlgorithm):
    """
    Jaccard Similarity Ranking Algorithm
    
    Computes set-based intersection over union for text tokens:
    Jaccard(A, B) = |A âˆ© B| / |A âˆª B|
    """
    
    def __init__(self, config: dict = None):
        super().__init__('jaccard', config)
        self.tech_skills = self._load_tech_skills()
        self.stopwords = self._load_stopwords()
    
    def _load_tech_skills(self) -> set:
        """Load technical skills for tracking and boosting"""
        return {
            'python', 'java', 'javascript', 'typescript', 'react', 'angular', 'vue',
            'nodejs', 'django', 'flask', 'spring', 'aws', 'azure', 'docker',
            'kubernetes', 'mongodb', 'postgresql', 'mysql', 'redis', 'git', 'c++',
            'c#', 'go', 'rust', 'php', 'ruby', 'swift', 'kotlin', 'scala', 'c',
            'html', 'css', 'tailwind', 'bootstrap', 'scikit-learn', 'tensorflow',
            'pytorch', 'fastapi', 'nextjs', 'nestjs', 'gcp', 'jenkins', 'terraform'
        }

    def _load_stopwords(self) -> set:
        """Load a standard set of common English and Vietnamese stopwords"""
        return {
            # English stopwords
            'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', 'arent', 'as', 'at',
            'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by', 'cant', 'cannot', 'could',
            'couldnt', 'did', 'didnt', 'do', 'does', 'doesnt', 'doing', 'dont', 'down', 'during', 'each', 'few', 'for', 'from',
            'further', 'had', 'hadnt', 'has', 'hasnt', 'have', 'havent', 'having', 'he', 'hed', 'hell', 'hes', 'her', 'here',
            'heres', 'hers', 'herself', 'him', 'himself', 'his', 'how', 'hows', 'i', 'id', 'ill', 'im', 'ive', 'if', 'in',
            'into', 'is', 'isnt', 'it', 'its', 'itself', 'lets', 'me', 'more', 'most', 'mustnt', 'my', 'myself', 'no', 'nor',
            'not', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'ought', 'our', 'ours', 'ourselves', 'out', 'over', 'own',
            'same', 'shant', 'she', 'shed', 'shell', 'shes', 'should', 'shouldnt', 'so', 'some', 'such', 'than', 'that',
            'thats', 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there', 'theres', 'these', 'they', 'theyd',
            'theyll', 'theyre', 'theyve', 'this', 'those', 'through', 'to', 'too', 'under', 'until', 'up', 'very', 'was',
            'wasnt', 'we', 'wed', 'well', 'were', 'weve', 'werent', 'what', 'whats', 'when', 'whens', 'where', 'wheres',
            'which', 'while', 'who', 'whos', 'whom', 'why', 'whys', 'with', 'wont', 'would', 'wouldnt', 'you', 'youd',
            'youll', 'youre', 'youve', 'your', 'yours', 'yourself', 'yourselves',
            # Vietnamese common words / stopwords
            'vÃ ', 'vá»›i', 'cho', 'cá»§a', 'lÃ ', 'cÃ¡c', 'nhá»¯ng', 'Ä‘á»ƒ', 'trong', 'ngoÃ i', 'ra', 'vÃ o', 'táº¡i', 'theo', 'nhÆ°',
            'Ä‘Æ°á»£c', 'bá»‹', 'bá»Ÿi', 'nÃ y', 'kia', 'Ä‘Ã³', 'nÃ o', 'sáº½', 'Ä‘Ã£', 'Ä‘ang', 'cáº§n', 'muá»‘n', 'cÃ³', 'lÃ m', 'má»™t',
            'nhiá»u', 'Ã­t', 'khoáº£ng', 'trÃªn', 'dÆ°á»›i'
        }
    
    def load_model(self):
        self.is_loaded = True
        logger.info("Jaccard Similarity analyzer initialized")
    
    def _tokenize(self, text: str) -> set:
        """Clean, tokenize and return a set of unique words excluding stopwords"""
        if not text:
            return set()
        
        # Lowercase and clean text
        text = text.lower()
        # Keep alphanumeric, plus, sharp, dot, hyphen
        text = re.sub(r'[^\w\s\-\+\.\#]', ' ', text)
        
        tokens = text.split()
        # Filter stopwords and very short tokens unless they are skills (like 'c', 'r', 'go')
        filtered_tokens = set()
        for t in tokens:
            if t in self.stopwords:
                continue
            if len(t) <= 1 and t not in {'c', 'r', 'go'}:
                continue
            filtered_tokens.add(t)
            
        return filtered_tokens
    
    def process_single(self, resume_text: str, job_description: str, 
                       position: str = None) -> dict:
        """Calculate Jaccard similarity between resume and job description"""
        if not self.is_loaded:
            self.load_model()
        
        try:
            resume_tokens = self._tokenize(resume_text)
            job_tokens = self._tokenize(job_description)
            
            if not resume_tokens or not job_tokens:
                return {
                    'algorithm': self.name,
                    'score': 0.0,
                    'details': {
                        'algorithm_type': 'Jaccard Similarity',
                        'resume_tokens_count': len(resume_tokens),
                        'job_tokens_count': len(job_tokens),
                        'intersection_count': 0,
                        'union_count': 0,
                        'matching_skills': []
                    }
                }
            
            # Standard token-level Jaccard
            intersection = resume_tokens.intersection(job_tokens)
            union = resume_tokens.union(job_tokens)
            jaccard_score = len(intersection) / len(union)
            
            # Skill-specific Jaccard (intersection/union of technical skills)
            resume_skills = resume_tokens.intersection(self.tech_skills)
            job_skills = job_tokens.intersection(self.tech_skills)
            
            if job_skills:
                skill_intersection = resume_skills.intersection(job_skills)
                skill_union = resume_skills.union(job_skills)
                skill_jaccard_score = len(skill_intersection) / len(skill_union) if skill_union else 0.0
                # Blend: give higher weight to skill-specific overlap
                final_score = (jaccard_score * 0.3) + (skill_jaccard_score * 0.7)
            else:
                skill_jaccard_score = 0.0
                final_score = jaccard_score
                
            # Normalize to reasonable range
            final_score = max(0.0, min(1.0, final_score))
            
            matching_skills = sorted(list(intersection.intersection(self.tech_skills)))
            
            logger.info(f"Jaccard - Token score: {jaccard_score:.4f}, Skill score: {skill_jaccard_score:.4f}, Final: {final_score:.4f}")
            
            return {
                'algorithm': self.name,
                'score': float(final_score),
                'details': {
                    'algorithm_type': 'Jaccard Similarity (Set-Based)',
                    'token_jaccard': float(jaccard_score),
                    'skill_jaccard': float(skill_jaccard_score),
                    'resume_tokens_count': len(resume_tokens),
                    'job_tokens_count': len(job_tokens),
                    'intersection_count': len(intersection),
                    'union_count': len(union),
                    'matching_skills': matching_skills,
                }
            }
            
        except Exception as e:
            logger.error(f"Jaccard similarity ranking failed: {e}", exc_info=True)
            return {'algorithm': self.name, 'score': 0.0, 'details': {'error': str(e)}}

