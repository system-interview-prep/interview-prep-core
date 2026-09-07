# matching/manager.py
from typing import List, Dict, Any
import concurrent.futures
import logging

# Similarity analyzers
from src.modules.matching.engine.algorithms.similarity.cosine_similarity import CosineSimilarityAnalyzer
try:
    # Ensure this import points to a strict-skill Jaccard, not cosine
    from src.modules.matching.engine.algorithms.similarity.jaccard_similarity import JaccardSimilarityAnalyzer
except ImportError:
    JaccardSimilarityAnalyzer = None
try:
    from src.modules.matching.engine.algorithms.similarity.bm25_analyzer import BM25Analyzer
except ImportError:
    BM25Analyzer = None

from src.modules.matching.engine.algorithms.similarity.ner_analyzer import NERAnalyzer
from src.modules.matching.engine.algorithms.similarity.requirements_analyzer import RequirementsAnalyzer
from src.modules.matching.engine.algorithms.similarity.must_have_analyzer import MustHaveAnalyzer

logger = logging.getLogger(__name__)


class AlgorithmManager:
    """Manages and orchestrates multiple ranking algorithms with distinct behaviors"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.algorithms: Dict[str, Any] = {}

        # Registry with distinct implementations/paths
        registry: Dict[str, Any] = {}

        # Similarity models
        registry['cosine'] = CosineSimilarityAnalyzer
        if BM25Analyzer:
            registry['bm25'] = BM25Analyzer
        if JaccardSimilarityAnalyzer:
            registry['jaccard'] = JaccardSimilarityAnalyzer
        else:
            registry['jaccard'] = CosineSimilarityAnalyzer  # Warn at init time

        # Entity extraction
        registry['requirements'] = RequirementsAnalyzer
        registry['must_have'] = MustHaveAnalyzer
        registry['ner'] = NERAnalyzer

        self.algorithm_registry = registry
        self.max_workers = self.config.get('max_workers', 4)

    def initialize_algorithms(self, algorithm_names: List[str]) -> None:
        """Initialize specified algorithms with distinct configurations"""
        for name in algorithm_names:
            if name in self.algorithm_registry and name not in self.algorithms:
                try:
                    logger.info(f"Initializing algorithm: {name}")
                    algorithm_class = self.algorithm_registry[name]
                    algorithm_config = dict(self.config.get(name, {}))  # copy

                    # Configure lexical and rule-based legacy algorithms.
                    if name == 'cosine':
                        algorithm_config.setdefault('ngrams', (1, 2))
                        algorithm_config.setdefault('stopwords', 'english')
                        algorithm_config.setdefault('boost_terms', True)
                        algorithm_config.setdefault('section_aware', True)

                    elif name == 'jaccard':
                        algorithm_config.setdefault('legacy_alias_for', 'bm25')
                        algorithm_config.setdefault('algorithm_label', 'BM25')

                    elif name == 'bm25':
                        algorithm_config.setdefault('mode', 'strict_skill_coverage')
                        algorithm_config.setdefault('must_have_weight', 0.60)
                        algorithm_config.setdefault('should_have_weight', 0.30)
                        algorithm_config.setdefault('bonus_weight', 0.10)
                        algorithm_config.setdefault('penalty_per_missing_must', 0.15)

                    elif name == 'ner':
                        algorithm_config.setdefault('entities', ['SKILL', 'TITLE', 'ORG', 'CERT', 'EDU', 'EXP_YEARS'])
                        algorithm_config.setdefault('return_must_have_ok', True)

                    elif name == 'must_have':
                        algorithm_config.setdefault('strict', True)

                    elif name == 'requirements':
                        algorithm_config.setdefault('must_have_weight', 0.55)
                        algorithm_config.setdefault('nice_to_have_weight', 0.25)
                        algorithm_config.setdefault('constraints_weight', 0.20)

                    if name == 'jaccard' and self.algorithm_registry['jaccard'] is CosineSimilarityAnalyzer:
                        logger.warning("JaccardAnalyzer not found; falling back to Cosine. Add matching/algorithms/similarity/jaccard_similarity.py for strict coverage scoring.")

                    self.algorithms[name] = algorithm_class(algorithm_config)
                    if hasattr(self, 'vector_db'):
                        self.algorithms[name].vector_db = self.vector_db
                    if hasattr(self.algorithms[name], 'load_model'):
                        self.algorithms[name].load_model()

                    logger.info(f"Algorithm {name} initialized successfully")

                except Exception as e:
                    logger.error(f"Failed to initialize algorithm {name}: {e}")
                    continue

    def process_resumes_parallel(self, resume_texts: List[str],
                                 job_description: str, algorithm_names: List[str],
                                 position: str = None, job_id: str = None,
                                 cv_id: str = None, options: dict = None) -> Dict[str, Any]:
        """Process resumes using multiple algorithms in parallel"""
        self.initialize_algorithms(algorithm_names)

        available_algorithms = [name for name in algorithm_names if name in self.algorithms]
        if not available_algorithms:
            raise Exception("No algorithms available for processing")

        logger.info(f"Processing {len(resume_texts)} resumes with {len(available_algorithms)} algorithms (job_id={job_id}, cv_id={cv_id})")

        results: Dict[str, Any] = {
            'metadata': {
                'total_resumes': len(resume_texts),
                'algorithms_used': available_algorithms,
                'job_position': position,
                'processing_timestamp': None
            },
            'individual_scores': {},
            'combined_results': [],
            'algorithm_performance': {}
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_algorithm = {}
            for alg_name in available_algorithms:
                alg = self.algorithms[alg_name]
                import inspect
                if hasattr(alg, 'process_batch'):
                    sig = inspect.signature(alg.process_batch)
                    kwargs = {}
                    if 'job_id' in sig.parameters:
                        kwargs['job_id'] = job_id
                    if 'cv_id' in sig.parameters:
                        kwargs['cv_id'] = cv_id
                    if 'chunk_level' in sig.parameters:
                        kwargs['chunk_level'] = options.get('chunk_level', True) if options else True
                    future = executor.submit(alg.process_batch, resume_texts, job_description, position, **kwargs)
                else:
                    sig = inspect.signature(alg.process_single)
                    kwargs = {}
                    if 'job_id' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        kwargs['job_id'] = job_id
                    if 'cv_id' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        kwargs['cv_id'] = cv_id
                    if 'chunk_level' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        kwargs['chunk_level'] = options.get('chunk_level', True) if options else True
                    future = executor.submit(
                        lambda a=alg, kw=kwargs: [a.process_single(rt, job_description, position, **kw) for rt in resume_texts]
                    )
                future_to_algorithm[future] = alg_name

            for future in concurrent.futures.as_completed(future_to_algorithm):
                alg_name = future_to_algorithm[future]
                try:
                    alg_results = future.result()
                    results['individual_scores'][alg_name] = alg_results
                    if hasattr(self.algorithms[alg_name], 'get_performance_metrics'):
                        results['algorithm_performance'][alg_name] = self.algorithms[alg_name].get_performance_metrics()
                    else:
                        results['algorithm_performance'][alg_name] = {}
                    logger.info(f"Completed processing with {alg_name}")
                except Exception as e:
                    logger.error(f"Algorithm {alg_name} failed: {e}")
                    results['individual_scores'][alg_name] = []
                    results['algorithm_performance'][alg_name] = {'error': str(e)}

        results['combined_results'] = self._combine_algorithm_results(
            results['individual_scores'], len(resume_texts)
        )

        return results

    def _resolve_weights(self) -> Dict[str, float]:
        """Resolve fusion weights for active scorers, then normalize."""
        weights = {
            'cosine': 0.20,
            'bm25': 0.10,
            'jaccard': 0.05,
            'requirements': 0.18,
            'must_have': 0.12,
            'ner': 0.10,
        }
        cfg_weights = self.config.get('weights', {})
        weights.update(cfg_weights)

        # Normalize positive weights
        s = sum(v for v in weights.values() if v > 0)
        if s > 0:
            for k, v in list(weights.items()):
                if v > 0:
                    weights[k] = v / s
        return weights

    def _combine_algorithm_results(self, individual_scores: Dict[str, List],
                                   total_resumes: int) -> List[Dict[str, Any]]:
        """Combine scores from multiple algorithms using a clean 3-pillar weighted architecture"""
        combined_results: List[Dict[str, Any]] = []

        # Weights of individual algorithms
        weights = self._resolve_weights()

        # Pillar definition mapping
        pillar_mapping = {
            'lexical': {'cosine', 'bm25', 'jaccard'},
            'requirements': {'requirements', 'must_have', 'ner'}
        }

        # Global weights for the pillars
        pillar_weights_cfg = self.config.get('pillar_weights', {})
        pillar_weights = {
            'lexical': float(pillar_weights_cfg.get('lexical', 0.30)),
            'requirements': float(pillar_weights_cfg.get('requirements', 0.30))
        }

        for resume_idx in range(total_resumes):
            resume_result = {
                'resume_index': resume_idx,
                'algorithm_scores': {},
                'combined_score': 0.0,
                'weighted_score': 0.0,
                'rank': 0,
                'details': {'contributions': []},
                'score_breakdown': {},
                'errors': []
            }

            # Collect active algorithms and their scores for the current resume
            active_scores = {}
            for alg_name, alg_results in individual_scores.items():
                if resume_idx < len(alg_results):
                    result = alg_results[resume_idx] or {}
                    if 'error' not in result:
                        raw = result.get('score', None)
                        if raw is None:
                            details = result.get('details', {})
                            raw = details.get('probability') or details.get('confidence') or 0.0
                        try:
                            score = float(raw)
                        except Exception:
                            logger.warning(f"{alg_name} returned non-numeric score for resume {resume_idx}. Defaulting to 0.")
                            score = 0.0
                        score = max(0.0, min(1.0, score))
                        alg_weight = float(weights.get(alg_name, 0.0))

                        resume_result['algorithm_scores'][alg_name] = {
                            'score': score,
                            'weight': alg_weight,
                            'details': result.get('details', {})
                        }
                        active_scores[alg_name] = {
                            'score': score,
                            'weight': alg_weight,
                            'details': result.get('details', {})
                        }
                    else:
                        resume_result['errors'].append({'algorithm': alg_name, 'error': result['error']})

            # Calculate scores for each pillar
            pillar_scores = {}
            active_pillars = []
            
            for pillar_name, alg_set in pillar_mapping.items():
                pillar_active_algs = [alg for alg in alg_set if alg in active_scores]
                if pillar_active_algs:
                    # Calculate weighted average of active algorithms in this pillar
                    sum_weighted_scores = 0.0
                    sum_weights = 0.0
                    for alg in pillar_active_algs:
                        w = active_scores[alg]['weight']
                        s = active_scores[alg]['score']
                        sum_weighted_scores += s * w
                        sum_weights += w
                    
                    # Fallback to simple average if all weights are 0
                    if sum_weights > 0:
                        pillar_scores[pillar_name] = sum_weighted_scores / sum_weights
                    else:
                        pillar_scores[pillar_name] = sum(active_scores[alg]['score'] for alg in pillar_active_algs) / len(pillar_active_algs)
                    
                    active_pillars.append(pillar_name)
                else:
                    pillar_scores[pillar_name] = 0.0

            # Calculate combined raw weighted score over active pillars
            sum_pillar_weighted = 0.0
            sum_pillar_weights = 0.0
            for p in active_pillars:
                sum_pillar_weighted += pillar_scores[p] * pillar_weights[p]
                sum_pillar_weights += pillar_weights[p]

            if sum_pillar_weights > 0:
                raw_combined = sum_pillar_weighted / sum_pillar_weights
            else:
                raw_combined = 0.0

            resume_result['weighted_score'] = raw_combined
            resume_result['combined_score'] = raw_combined

            # Gather penalties across all active algorithms
            missing_must_count = 0
            missing_must_list = []
            constraints_ok = True
            
            for alg_name, data in active_scores.items():
                details = data.get('details', {})
                if 'missing_must_count' in details:
                    try:
                        missing_must_count = max(missing_must_count, int(details.get('missing_must_count', 0) or 0))
                    except Exception:
                        pass
                if 'missing_must_have' in details:
                    missing_must_list = list(set(missing_must_list + details.get('missing_must_have', [])))
                if details.get('constraints_ok') is False:
                    constraints_ok = False

            # Ensure missing_must_count matches our list length if list is populated
            if missing_must_list:
                missing_must_count = max(missing_must_count, len(missing_must_list))

            # Calculate soft must-have penalty
            must_have_multiplier = 1.0
            if missing_must_count > 0:
                per_missing = float(self.config.get('must_have_penalty_per_missing', 0.04))
                floor = float(self.config.get('must_have_penalty_floor', 0.75))
                must_have_multiplier = max(floor, 1.0 - (missing_must_count * per_missing))
                resume_result['combined_score'] *= must_have_multiplier

            # Calculate constraints penalty
            constraints_multiplier = 1.0
            if not constraints_ok:
                constraints_multiplier = 0.92
                resume_result['combined_score'] *= constraints_multiplier

            # Construct structured score breakdown
            lexical_weight_norm = pillar_weights['lexical'] / sum_pillar_weights if sum_pillar_weights > 0 and 'lexical' in active_pillars else 0.0
            req_weight_norm = pillar_weights['requirements'] / sum_pillar_weights if sum_pillar_weights > 0 and 'requirements' in active_pillars else 0.0

            resume_result['score_breakdown'] = {
                'lexical': {
                    'score': float(pillar_scores['lexical']),
                    'weight': float(lexical_weight_norm),
                    'active': 'lexical' in active_pillars
                },
                'requirements': {
                    'score': float(pillar_scores['requirements']),
                    'weight': float(req_weight_norm),
                    'active': 'requirements' in active_pillars
                },
                'penalties': {
                    'missing_must_count': int(missing_must_count),
                    'missing_must_list': missing_must_list,
                    'must_have_penalty_multiplier': float(must_have_multiplier),
                    'constraints_ok': bool(constraints_ok),
                    'constraints_penalty_multiplier': float(constraints_multiplier)
                }
            }

            # Enrich requirements breakdown with specific sub-scores from RequirementsAnalyzer if available
            for alg_name, data in active_scores.items():
                if alg_name in ('requirements', 'must_have'):
                    details = data.get('details', {})
                    resume_result['score_breakdown']['requirements'].update({
                        'must_have_score': float(details.get('must_have_score', 1.0)),
                        'nice_to_have_score': float(details.get('nice_to_have_score', 1.0)),
                        'constraints_score': float(details.get('constraints_score', 1.0)),
                        'required_years_experience': int(details.get('required_years_experience', 0)),
                        'resume_years_experience': int(details.get('resume_years_experience', 0)),
                        'experience_ok': bool(details.get('experience_ok', True)),
                        'missing_nice_to_have': details.get('missing_nice_to_have', []),
                        'missing_constraints': details.get('missing_constraints', [])
                    })
                    break

            # Populate contributions for backward-compatibility details mapping
            for p_name, score in pillar_scores.items():
                if p_name in active_pillars:
                    w_norm = pillar_weights[p_name] / sum_pillar_weights
                    resume_result['details']['contributions'].append({
                        'alg': f"pillar_{p_name}",
                        'score': float(score),
                        'weight': float(w_norm)
                    })

            combined_results.append(resume_result)

        combined_results.sort(key=lambda x: x['combined_score'], reverse=True)
        for idx, result in enumerate(combined_results):
            result['rank'] = idx + 1

        return combined_results

    def get_algorithm_status(self) -> Dict[str, Any]:
        status = {
            'available_algorithms': list(self.algorithm_registry.keys()),
            'loaded_algorithms': list(self.algorithms.keys()),
            'algorithm_details': {}
        }
        for name, algorithm in self.algorithms.items():
            if hasattr(algorithm, 'get_performance_metrics'):
                status['algorithm_details'][name] = algorithm.get_performance_metrics()
            else:
                status['algorithm_details'][name] = {}
        return status

    def cleanup(self) -> None:
        for algorithm in self.algorithms.values():
            try:
                if hasattr(algorithm, 'cleanup'):
                    algorithm.cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up algorithm: {e}")
        self.algorithms.clear()
        logger.info("Algorithm manager cleaned up")

