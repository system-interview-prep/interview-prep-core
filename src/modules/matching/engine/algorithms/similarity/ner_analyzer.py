try:
    import spacy
except ImportError:
    spacy = None
from collections import Counter
import re
from ..base_algorithm import BaseAlgorithm
import logging

logger = logging.getLogger(__name__)

class NERAnalyzer(BaseAlgorithm):
    """Named Entity Recognition for skill and experience extraction"""
    
    def __init__(self, config: dict = None):
        super().__init__('ner', config)
        self.nlp = None
        self.skill_patterns = self._load_skill_patterns()
        self.experience_patterns = self._load_experience_patterns()
    
    def _load_skill_patterns(self) -> dict:
        """Load predefined skill patterns"""
        return {
            'programming_languages': [
                'python', 'java', 'javascript', 'typescript', 'c++', 'c#', 'go', 'rust',
                'php', 'ruby', 'swift', 'kotlin', 'scala', 'r', 'matlab'
            ],
            'frameworks': [
                'react', 'angular', 'vue', 'django', 'flask', 'spring', 'express',
                'laravel', 'rails', 'tensorflow', 'pytorch', 'scikit-learn'
            ],
            'databases': [
                'mysql', 'postgresql', 'mongodb', 'redis', 'cassandra', 'elasticsearch',
                'oracle', 'sqlite', 'dynamodb'
            ],
            'cloud_platforms': [
                'aws', 'azure', 'gcp', 'google cloud', 'docker', 'kubernetes',
                'jenkins', 'terraform', 'ansible'
            ],
            'tools': [
                'git', 'jira', 'confluence', 'slack', 'figma', 'adobe', 'photoshop',
                'illustrator', 'sketch', 'invision'
            ]
        }
    
    def _load_experience_patterns(self) -> list:
        """Load experience extraction patterns"""
        return [
            r'(\d+)\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)',
            r'(\d+)\+?\s*(?:years?|yrs?)',
            r'over\s*(\d+)\s*(?:years?|yrs?)',
            r'more than\s*(\d+)\s*(?:years?|yrs?)'
        ]
    
    def load_model(self):
        """Load spaCy model"""
        try:
            if spacy is None:
                raise Exception("spaCy is not installed. Install dependencies from requirements.txt")
            logger.info("Loading spaCy model")
            # Try to load different models in order of preference
            models = ['en_core_web_lg', 'en_core_web_md', 'en_core_web_sm']
            
            for model_name in models:
                try:
                    self.nlp = spacy.load(model_name)
                    logger.info(f"Loaded spaCy model: {model_name}")
                    break
                except OSError:
                    continue
            
            if self.nlp is None:
                raise Exception("No spaCy model found. Please install: python -m spacy download en_core_web_sm")
            
            self.is_loaded = True
            
        except Exception as e:
            logger.error(f"Failed to load spaCy model: {e}")
            raise
    
    def _extract_skills(self, text: str) -> dict:
        """Extract skills from text"""
        text_lower = text.lower()
        extracted_skills = {}
        
        for category, skills in self.skill_patterns.items():
            found_skills = []
            for skill in skills:
                if skill in text_lower:
                    # Count occurrences
                    count = text_lower.count(skill)
                    found_skills.append({'skill': skill, 'count': count})
            
            if found_skills:
                extracted_skills[category] = found_skills
        
        return extracted_skills

    def _flatten_skill_names(self, extracted_skills: dict) -> set:
        names = set()
        for skill_list in (extracted_skills or {}).values():
            for skill_data in skill_list:
                if isinstance(skill_data, dict) and skill_data.get('skill'):
                    names.add(str(skill_data['skill']).lower())
                elif isinstance(skill_data, str):
                    names.add(skill_data.lower())
        return names

    def _infer_must_have_skills(self, job_description: str, job_skills: dict) -> list:
        """Infer mandatory skills from explicit must-have sections or all required JD skills."""
        text = (job_description or '').lower()
        all_job_skills = sorted(self._flatten_skill_names(job_skills))
        if not all_job_skills:
            return []

        must_markers = ['must have', 'must-have', 'required', 'requirements', 'báº¯t buá»™c', 'yÃªu cáº§u']
        nice_markers = ['nice to have', 'nice-to-have', 'preferred', 'bonus', 'Æ°u tiÃªn']

        must_section = text
        marker_positions = [text.find(marker) for marker in must_markers if marker in text]
        if marker_positions:
            start = min(pos for pos in marker_positions if pos >= 0)
            end_candidates = [text.find(marker, start + 1) for marker in nice_markers if text.find(marker, start + 1) >= 0]
            end = min(end_candidates) if end_candidates else len(text)
            must_section = text[start:end]

        inferred = [skill for skill in all_job_skills if skill in must_section]
        return inferred or all_job_skills
    
    def _extract_experience(self, text: str) -> dict:
        """Extract experience information"""
        experience_info = {'years': [], 'roles': [], 'companies': []}
        
        # Extract years of experience
        for pattern in self.experience_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            experience_info['years'].extend([int(match) for match in matches])
        
        # Extract entities using spaCy
        doc = self.nlp(text)
        
        for ent in doc.ents:
            if ent.label_ == "ORG":
                experience_info['companies'].append(ent.text)
            elif ent.label_ in ["PERSON", "JOB_TITLE"]:
                # Simple heuristic for job titles
                if any(keyword in ent.text.lower() for keyword in 
                      ['engineer', 'developer', 'manager', 'analyst', 'scientist', 'designer']):
                    experience_info['roles'].append(ent.text)
        
        return experience_info
    
    def _calculate_skill_match_score(self, resume_skills: dict, job_description: str) -> float:
        """Calculate skill matching score"""
        job_desc_lower = job_description.lower()
        total_matches = 0
        total_required = 0
        
        for category, skills in self.skill_patterns.items():
            required_skills = [skill for skill in skills if skill in job_desc_lower]
            total_required += len(required_skills)
            
            if category in resume_skills:
                resume_skill_names = [skill['skill'] for skill in resume_skills[category]]
                matches = len(set(required_skills) & set(resume_skill_names))
                total_matches += matches
        
        return total_matches / max(total_required, 1)
    
    def process_single(self, resume_text: str, job_description: str, 
                      position: str = None) -> dict:
        """Process single resume with NER"""
        if not self.is_loaded:
            self.load_model()
        
        try:
            # Extract information from resume
            resume_skills = self._extract_skills(resume_text)
            resume_experience = self._extract_experience(resume_text)
            
            # Extract required skills from job description
            job_skills = self._extract_skills(job_description)
            resume_skill_names = self._flatten_skill_names(resume_skills)
            must_have_skills = self._infer_must_have_skills(job_description, job_skills)
            missing_must_have = [skill for skill in must_have_skills if skill not in resume_skill_names]
            must_have_ok = len(missing_must_have) == 0
            
            # Calculate skill match score
            skill_score = self._calculate_skill_match_score(resume_skills, job_description)
            
            # Calculate experience score
            max_years = max(resume_experience['years']) if resume_experience['years'] else 0
            experience_score = min(1.0, max_years / 10.0)  # Normalize to max 10 years
            
            # Combined score (weighted average)
            combined_score = (skill_score * 0.7) + (experience_score * 0.3)
            
            return {
                'algorithm': self.name,
                'score': float(combined_score),
                'details': {
                    'extracted_skills': resume_skills,
                    'extracted_experience': resume_experience,
                    'skill_match_score': float(skill_score),
                    'experience_score': float(experience_score),
                    'max_years_experience': max_years,
                    'total_skill_categories': len(resume_skills),
                    'companies_mentioned': len(set(resume_experience['companies'])),
                    'job_required_skills': must_have_skills,
                    'missing_must_have': missing_must_have,
                    'missing_must_count': len(missing_must_have),
                    'must_have_ok': must_have_ok,
                }
            }
            
        except Exception as e:
            logger.error(f"NER processing failed: {e}")
            raise

