from typing import Final

CANDIDATE: Final = "CANDIDATE"
ADMIN: Final = "ADMIN"
QUESTION_AUTHOR: Final = "QUESTION_AUTHOR"
# Retired 28/10: reviewing and final approval are done by ADMIN. Kept so old
# assignments still parse; no permission is granted by them and they cannot be assigned.
QUESTION_REVIEWER: Final = "QUESTION_REVIEWER"
DATA_CURATOR: Final = "DATA_CURATOR"
QUESTION_BANK_ADMIN: Final = "QUESTION_BANK_ADMIN"
RETIRED_ROLES: Final = frozenset({QUESTION_REVIEWER, QUESTION_BANK_ADMIN})
INTERNAL_ACTORS: Final = frozenset({ADMIN, QUESTION_AUTHOR, DATA_CURATOR})
# Every value that may be stored; ASSIGNABLE_ROLES is what an admin can give.
USER_ROLES: Final = frozenset({CANDIDATE, *INTERNAL_ACTORS, *RETIRED_ROLES})
ASSIGNABLE_ROLES: Final = frozenset({CANDIDATE, *INTERNAL_ACTORS})
