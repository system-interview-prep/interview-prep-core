from abc import ABC, abstractmethod
from typing import List, Dict, Any, Tuple
import logging
import time

logger = logging.getLogger(__name__)

class BaseAlgorithm(ABC):
    """Base class for all resume ranking algorithms"""
    
    def __init__(self, name: str, config: Dict[str, Any] = None):
        self.name = name
        self.config = config or {}
        self.is_loaded = False
        self.model = None
        self._performance_metrics = {
            'total_processed': 0,
            'total_time': 0,
            'average_time': 0,
            'errors': 0
        }
    
    @abstractmethod
    def load_model(self) -> None:
        """Load the algorithm's model/components"""
        pass
    
    @abstractmethod
    def process_single(self, resume_text: str, job_description: str, 
                       position: str = None, job_description_id: str = None, cv_id: str = None) -> dict:
        """Process a single resume against a job description"""
        raise NotImplementedError("Each algorithm must implement process_single")

    def process_batch(self, resume_texts: list, job_description: str, 
                      position: str = None, job_description_id: str = None, cv_id: str = None) -> list:
        """Process multiple resumes in batch, utilizing any algorithm-level optimizations."""
        if not self.is_loaded:
            self.load_model()
            
        results = []
        start_time = time.time()
        
        try:
            for i, resume_text in enumerate(resume_texts):
                try:
                    import inspect
                    sig = inspect.signature(self.process_single)
                    kwargs = {}
                    if 'job_description_id' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        kwargs['job_description_id'] = job_description_id
                    if 'cv_id' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        kwargs['cv_id'] = cv_id
                    result = self.process_single(resume_text, job_description, position, **kwargs)
                    result['resume_index'] = i
                    results.append(result)
                    self._performance_metrics['total_processed'] += 1
                except Exception as e:
                    logger.error(f"Error processing resume {i} with {self.name}: {e}")
                    self._performance_metrics['errors'] += 1
                    results.append({
                        'resume_index': i,
                        'score': 0.0,
                        'error': str(e),
                        'algorithm': self.name
                    })
            
            processing_time = time.time() - start_time
            self._performance_metrics['total_time'] += processing_time
            self._performance_metrics['average_time'] = (
                self._performance_metrics['total_time'] / 
                max(self._performance_metrics['total_processed'], 1)
            )
            
            logger.info(f"{self.name} processed {len(resume_texts)} resumes in {processing_time:.2f}s")
            
        except Exception as e:
            logger.error(f"Batch processing failed for {self.name}: {e}")
            raise
        
        return results
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get algorithm performance statistics"""
        return {
            'algorithm': self.name,
            'is_loaded': self.is_loaded,
            'performance': self._performance_metrics.copy()
        }
    
    def cleanup(self) -> None:
        """Clean up resources"""
        if self.model:
            del self.model
        self.model = None
        self.is_loaded = False

