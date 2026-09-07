from .requirements_analyzer import RequirementsAnalyzer


class MustHaveAnalyzer(RequirementsAnalyzer):
    """Backward-compatible alias for the structured requirements analyzer."""

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.name = 'must_have'

