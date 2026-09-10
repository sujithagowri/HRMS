from abc import ABC, abstractmethod


class CandidateSourceConnector(ABC):
    source_name = None

    @abstractmethod
    def search(self, requirements, filters=None):
        """Return normalized candidate dictionaries."""
