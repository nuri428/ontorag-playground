"""Domain-neutral query-type router.

Classifies a natural-language question as state / incremental / multi-hop
without any domain-specific knowledge.
"""
from __future__ import annotations

import re
from enum import Enum


class QueryType(Enum):
    STATE = "state"           # "What is X?" — plain SPARQL
    INCREMENTAL = "incremental"  # "What was added recently?" — ingestedAt filter
    MULTI_HOP = "multihop"   # multi-hop traversal needed


_INCREMENTAL = [
    r"최근|recently|new(ly)?|just\s+added|this\s+week|이번\s*주|오늘|today|방금",
    r"added\s+since|추가(된|됐|됩|되었)",
    r"since\s+\w+|from\s+\w+\s+ago",
]

_MULTI_HOP = [
    r"통해|through|via|연결|linked|related\s+to|path",
    r"(that|who|which)\s+(appeared|participated|performed|contains|has|was\s+in)",
    r"같이|함께",
    r"(나온|출연한|참여한).*(다른|또\s*다른)",
    r"(appeared|participated)\s+in.*other",
]


def route_question(question: str) -> QueryType:
    q = question.lower()
    for pat in _INCREMENTAL:
        if re.search(pat, q, re.IGNORECASE):
            return QueryType.INCREMENTAL
    for pat in _MULTI_HOP:
        if re.search(pat, q, re.IGNORECASE):
            return QueryType.MULTI_HOP
    return QueryType.STATE
